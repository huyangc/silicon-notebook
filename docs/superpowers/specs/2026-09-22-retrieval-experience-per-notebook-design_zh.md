# 设计草案：检索策略经验库按笔记本分区

状态：已批准（2026-09-22，用户裁决「Q1–Q5 都按推荐来，先做 PR-1」，并把单库触发阈值从草案的 20
改为 **10**：「10 次就已经有很长的轨迹，能够看出一些问题了」）。基线 `7db67fc1`（本 worktree HEAD，
master 之上无其它提交）。实现计划 `docs/superpowers/plans/2026-09-22-retrieval-experience-per-notebook.md`。
上游设计真源 `docs/superpowers/specs/2026-08-18-agentic-memory-design.md`；本文是对其 §12-Q3
「策略经验作用域：全局库」裁决的**翻案**，落地后作为该文档 §10.4 偏离登记的来源。

## 0. 需求与结论

用户需求（2026-09-22 原话要点）：agent memory 应当 **by notebook**——每个笔记本里的 agent 对
自己的知识库越来越熟悉，越用越好。

现状与需求的错位（对账见上一轮评审）：

- 「熟悉这个库」的信号被拆到三处：共享底座（库形状/核心概念/语料缺口，笔记本级）、
  私有覆盖层（检索心得/查不到的，**每人一份**）、检索策略经验库（**部署级全局**、无主题）。
- 最直接表达「在这个库里怎么查更有效」的是覆盖层 `retrieval_notes`，但它只归个人，
  §12-Q2 已登记代价「A 的经验帮不到 B」；回收通道被指向 P2 全局经验库，而全局经验库按
  构造说不出任何一句关于「这个库」的话。
- 结论：经验库已经是封闭词表、结构上不含主题，**按 `notebook_id` 分区不会引入 Q2 担心的
  话题泄露，却能让「这个库里哪种动作好用」被全体成员共享并随使用积累**。全局分区保留，
  作为新库冷启动的回退。

本文只改经验库（P2/P4 的 B-经验层）。P1 笔记本理解与 P3 用户偏好不动；P1 巡固链路在本机
从未跑成过一次（`agent_profile_jobs` 全部 `runs=0`）是另一件事，另行验证，不在本文范围。

## 1. 现状核实（行号对本 worktree）

- 表 `retrieval_experiences`（SQLite v54 / PG 0032）无 `notebook_id`、无 owner 列，主键
  `id = "rx_" + sha256({situation, action})[:32]` 内容寻址
  （`backend/app/services/retrieval_experience_projection.py:627`）。
- 蒸馏取数 `recent_completed_ask_runs(job_limit, step_limit)` 刻意无 notebook/user 谓词，
  只取 `mode='reasoning'`（`backend/app/repositories/postgres/ask_state_store.py:543`，
  SQLite 镜像 `sqlite/ask_state_store.py:628`）；`project_run_row` 刻意不带 `notebook_id`
  （`backend/app/repositories/ports.py:5007`）。
- 触发是进程内全局计数（`retrieval_experience_trigger`，默认 40），
  `note_ask_completed()` **刻意不收任何参数**（`retrieval_experience_job.py:205`，docstring
  明言「知道是谁的 ask 的触发器离记录它只差一次重构」）。
- 单飞是进程内布尔；每批最多 4 个情境 × 3 条相似旧条目；ADD 需 ≥2 个不同 run；
  落库后无条件 `evict_to_limit(300)`。
- 注入侧 `_cached_experiences(store)` 全表读 + 按 `version_signal()` memo
  （`backend/app/services/reasoning_retrieval.py:971`）；三个消费点：任务级注入
  （`:4576`）、consult_memory（`:6735`）、步级零命中提示（`:7155`）；采用回写 `note_adopted`
  （`:7612`）。注入判据 `experience_wiring_active` = `RETRIEVAL_EXPERIENCE_INJECT_ENABLED`
  （默认 **false**）∧ store 在。
- 注入点已经拿着当前 `notebook_id`（P1 理解块同一处 `read_blocks(notebook_id, ...)`，
  `reasoning_retrieval.py:4534`）。全局问答引擎 `global_run.py`/`global_ask.py` 不构造
  `ReasoningRetriever`、不接 `retrieval_experiences`，不受本文影响。
- 隐私守卫 `backend/tests/test_retrieval_experience_privacy_guard.py` 判据二在 projection /
  job / block 三个模块里**禁止出现 `notebook_id` 这个名字**（禁键表第 106 行附近，理由
  「租户 id……某人在某库问过某类问题就能被拼回来」）。
- 笔记本删除的直删登记表 `backend/app/services/notebook_delete_tables.py:255`
  （`DirectTable("agent_notebook_profile", "two")` 同款）；深拷贝表清单在两侧
  `sharing_store.py` 的注释里明言经验库「没有 notebook_id 列所以碰不到」；
  `scripts/merge_dbs.py:153` 以 `INSERT OR IGNORE` 按内容寻址 id 并集，合并后按运行时
  淘汰序收容到 300。
- 本机 PG 库：`retrieval_experiences` 0 行；45 次 reasoning 完成提问分散在多次重启里，
  从未触发过一批。

## 2. 目标与非目标

目标：

1. 经验条目按 `notebook_id` 分区；一个笔记本的条目只由该笔记本的 run 蒸出、只注入该笔记本
   的 run，全体成员共享。
2. 保留全局分区（`notebook_id=''`）作为回退：新库或条目不足时补齐名额。
3. 每个笔记本按自己的用量节奏蒸馏，冷库零成本。
4. 隐私保证的形态不变：模型仍只见数字与封闭词；条目仍无主题、无来源、无用户。

非目标（本文不做，登记）：

- 不改情境指纹的八个键、动作词表、prompt 措辞、相似度算法。
- 不把私有覆盖层 `retrieval_notes` 共享化（撞 Q2 红线，需另一份无主题形态设计）。
- 不做经验库的通用管理面（P4 §10.3 第 7 条口径）；界面只加最小观测入口（§8）。
- 不改 MCP 工具面；`get_notebook_profile` 不带经验条目。
- 不跨库读挂载参考库的分区（镜像 P1「画像只属于 active notebook」）。

## 3. 数据模型

新增一列，不新增表：

| 列 | 类型 | 语义 |
| --- | --- | --- |
| `notebook_id` | `TEXT NOT NULL DEFAULT ''`（PG `text COLLATE "C"`） | `''` = 全局分区；非空 = 该笔记本的分区 |

- 迁移：SQLite v79 `add_column_if_missing` + 非唯一索引
  `idx_retrieval_experiences_notebook(notebook_id)`；PG 0059 同款 `ALTER TABLE ... ADD COLUMN`
  + `CREATE INDEX`。既有行全部落入全局分区，**不重算 id**。
- 主键仍内容寻址：`notebook_id=''` 时哈希输入与今天**逐字节相同**（既有 id 不变，
  `merge_dbs` 并集语义不变）；非空时输入为 `{"partition", "situation", "action"}` 排序序列化
  （键名是 `partition` 而非 `notebook_id`：隐私守卫判据二连 projection 模块里的字符串常量也扫，
  而这个键只是哈希输入，不落库、不渲染，两侧部署跑同一份代码，T1 实施时登记）。
  同一 (情境, 动作) 在不同分区是不同的行，这是分区的定义。
- 唯一约束不变（仍只有主键），shadow 不变量的 unique surface 计数不变；非唯一索引不计入。
- 上限：全局分区沿用 `RETRIEVAL_EXPERIENCE_MAX_ENTRIES=300`；每个笔记本分区
  `RETRIEVAL_EXPERIENCE_NOTEBOOK_MAX_ENTRIES`（默认 100，§9）。`evict_to_limit` 改为按分区
  收容；表总量上界 = 300 + 100 × 有流量的笔记本数，登记为可接受。
- `version_signal()` 仍是全表级信号（mutation revision, 行数, MAX(updated_at)）：写入稀少，
  一次写入让所有分区缓存失效可接受。

## 4. 蒸馏链路

两条链路，同一份 worker，按分区参数化：

| | 全局链路（不变） | 笔记本链路（新增） |
| --- | --- | --- |
| 触发计数 | 部署级进程内计数，阈值 `RETRIEVAL_EXPERIENCE_TRIGGER`（40） | 按 `notebook_id` 的进程内计数字典，阈值 `RETRIEVAL_EXPERIENCE_NOTEBOOK_TRIGGER`（**10**，用户裁决） |
| 取数 | `recent_completed_ask_runs(job_limit=40, step_limit=600)` 无谓词 | 同方法加 `notebook_id=?` 谓词，取该库最近 40 条 reasoning 完成 run |
| 既有条目 | `read_partition('')` | `read_partition(notebook_id)` |
| 写入 | `notebook_id=''` | `notebook_id=<本库>` |
| 淘汰 | 全局分区 ≤300 | 该分区 ≤100 |

要点：

- `note_ask_completed(notebook_id)`：签名从零参数改为收 `notebook_id`。这是对 P2 一条明文
  登记的推翻（§12 偏离①）。收到后**两个计数同时 +1**：全局计数与该库计数；各自到阈值各自
  排一批。同一次完成最多引发两批，实际频率见 §11 成本。
- 单飞：保留一个进程内 `_running` 槽位（同一时刻只跑一批，避免两条链路并发写同一张表，
  也避免多库同时到阈值时并发烧模型），待跑分区排进有界队列（去重、按到达顺序）；
  跑完后原子复查队列与积压计数（沿用 `_maybe_requeue` 形态）。
- 计数字典只在有流量的笔记本上有键；重启归零（与今天同款登记，§12 偏离③）。
- 蒸馏 worker 内部**只把分区 id 当参数**：取数谓词与写入列用它，`project_run` /
  `RunObservation` / `ObservedRun` / `render_observations` / `render_existing` / prompt 均
  **不含**它。模型看到的输入与今天逐字节相同。
- `_MIN_SUPPORTING_RUNS=2`、`_MAX_SITUATIONS_PER_BATCH=4`、`_MAX_SIMILAR_ENTRIES=3`、
  相似度地板 0.5 全部沿用。
- 单库链路阈值 10 **小于**批读取上限 40：每批读该库最近 40 条 run，相邻两批有 30 条重叠。
  这是刻意的——一批看到的样本越多越容易在 ≥2 run 上看出模式，而重叠 run 的重复计数由
  provenance 去重吸收（`support` 只对尚未出现在条目 provenance 里的 run 递增）。全局链路仍是
  40/40 对齐。
- 事件 `retrieval_experience_distilled` 增加 `partition: "global" | "notebook"` 一个封闭值，
  **不带 notebook id**（沿用 `_emit` 只记计数的口径：事件流带 id 会让运维按时间拼出某库的
  提问形状变化）。
- 手动触发：提供 `distill_now(notebook_id)`（供 §8 界面按钮），**自行**过
  `distillation_wiring_active`（job 模块 `start()` docstring 已预警：调用方必须自己闸）。

## 5. 注入链路

- `_cached_experiences(store, notebook_id)`：分区缓存，键 `(notebook_id, version_signal)`，
  有界（LRU，64 个分区）；全局分区单独一份缓存。
- 选择顺序：先在本库分区上跑 `select_experiences`（top-k=3、地板、同动作唯一），名额未满时
  再从全局分区补齐，跨分区仍保持同动作唯一。本库条目**优先**是产品规则：它们才是「这个库」
  的经验。
- consult_memory（差集面）与步级零命中提示（`worst_experience_for`）读同一份「本库 ∪ 全局」
  合并列表，逻辑不变。
- 采用回写 `note_adopted(ids)` 不变：id 已带分区。
- trace 步 `experience` 的 detail 增加 `notebook_entries`（送达行里来自本库分区的条数），
  不带正文。
- 注入闸仍是 `RETRIEVAL_EXPERIENCE_INJECT_ENABLED` 一把；本文不改默认值（§13-Q3）。

## 6. 隐私与守卫

保证的形态不变，边界收窄一个粒度：**今天是「不知道是谁、也不知道是哪个库」，改后是
「不知道是谁，知道是哪个库，且只有这个库的成员读得到」**。条目本身仍无主题、无来源、无
用户；能被推出的只是「这个库里出现过某种形状的问题、某个动作在这里好不好用」，读者本来就是
这个库的成员。

守卫改动（`test_retrieval_experience_privacy_guard.py`）：

- 判据二禁键表**保留** `notebook_id`，但对 job 模块加一条**显式白名单**：`notebook_id` 只许
  作为函数形参/实参名出现；`row["notebook_id"]`、`.get("notebook_id")`、把它塞进任何
  dataclass 字段、字符串拼接进 prompt，一律仍被判违规。projection 与 block 两个模块**零豁免**。
- projection 模块唯一需要知道分区的函数是 `experience_id`（分区是内容寻址输入的一部分）。它的
  形参命名为 `partition` 而不是 `notebook_id`：这不是绕守卫，而是语义上它确实不是任何 run 的
  属性，projection 模块因此保持对禁键表零豁免；job 模块以 `partition=notebook_id` 调用。
- 新增判据八（静态）：`RunObservation`/`ObservedRun` 的字段集合不含 `notebook_id`；
  `project_run_row` 的返回键集合不含它。
- 新增判据九（运行时）：用一个特征明显的 notebook id 跑一遍分区蒸馏（假模型），断言
  `render_observations`/`render_existing` 输出与发给模型的完整 prompt 中都不含该 id。
- rationale 的 id 形状绊线 `_ID_SHAPE = [0-9a-fA-F]{16,}` 抓不到 `nb-` 前缀的短 id
  （本机样本 `nb-a73f16940c` 只有 10 位 hex）。追加一条 `\bnb-[0-9a-f]{6,}` 绊线。模型按
  构造见不到 id，这是纵深防御，不是主保证。
- 读侧访问控制：注入只读当前 run 的 `notebook_id` 分区，调用方已经通过了该库的读权校验
  （run 本身就是它的提问）。不新增任何读端点（界面读取走 §8 的 P1 面板端点，沿用其读权判据）。

## 7. 生命周期

| 事件 | 处理 | 依据 |
| --- | --- | --- |
| 笔记本删除 | `notebook_delete_tables.py` 登记 `DirectTable("retrieval_experiences", ...)`，按 `notebook_id=?` 直删；`''` 行永不匹配 | 镜像 `agent_notebook_profile` |
| 深拷贝 | **不复制**分区行（副本从零攒） | 镜像 P1「深拷贝不复制画像行」；两侧 `sharing_store.py` 注释改写 |
| 成员移出/降级 | 无操作：条目归库不归人 | 分区语义 |
| 合库 `merge_dbs.py` | 并集语义不变（内容寻址 id 已含分区）；合并后**按分区**收容到各自上限 | `_evict_experiences_to_limit` 改按分区 |
| 挂载参考库 | 不读参考库分区 | §2 非目标 |
| 全局问答 | 不采样、不注入（引擎不接经验库，现状） | §1 |

## 8. 界面（最小）

在 P1 面板「AI 对这个库的理解」共享底座半侧下新增折叠小节（词汇待过
`scripts/check_ui_vocabulary.py`，「蒸馏/打法」不得直出）：

- 计数与最近更新时间：「这个库攒下的检索经验：N 条，最近更新 …」。
- 「立即整理」按钮（可写成员）：调用 `distill_now(notebook_id)`；按下有可见变化、结果落在
  按钮自身或紧邻处（`AGENTS.md` Interactive feedback 基线）；busy/关闭态给出原因。
- 「清空」按钮（owner）：删该库分区，二次确认。
- 条目列表（动作的界面词 + 好/不好 + 理由一句 + 支持数）：**待拍板**（§13-Q2）。

端点挂在既有 `GET/POST .../understanding` 家族下，复用其 `enabled` 与读权判据，不新开鉴权面。

## 9. 配置项

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `RETRIEVAL_EXPERIENCE_ENABLED` | true（不变） | 两条蒸馏链路共用的总闸 |
| `RETRIEVAL_EXPERIENCE_INJECT_ENABLED` | false（不变，§13-Q3） | 注入闸，两个分区共用 |
| `RETRIEVAL_EXPERIENCE_TRIGGER` | 40（不变） | 全局链路阈值 |
| `RETRIEVAL_EXPERIENCE_NOTEBOOK_TRIGGER` | **10**，`ge=1` | 单库链路阈值：该库累计完成的 reasoning 提问数 |

单库分区上限 `RETRIEVAL_EXPERIENCE_NOTEBOOK_MAX_ENTRIES = 100` 是 `ports.py` 协议常量，镜像既有的
`RETRIEVAL_EXPERIENCE_MAX_ENTRIES = 300`（全局上限也不是 env），`merge_dbs.py` 按同一常量收容。

不新增「按库分区」独立布尔：分区是数据模型，不是可选行为；`RETRIEVAL_EXPERIENCE_NOTEBOOK_TRIGGER`
调到极大等价于只跑全局链路。

## 10. 文档与守卫同步清单

- `docs/superpowers/specs/2026-08-18-agentic-memory-design.md`：§12-Q3 追加翻案记录；新增
  §10.4 偏离登记（§12 本文）。
- `docs/product-and-api_zh.md` / `docs/product-and-api.md`「检索策略经验」节：作用域、优先序、
  分区上限；数值只在这里登记。
- `docs/deployment-and-configuration_zh.md` / `.md`：两个新变量，`RETRIEVAL_EXPERIENCE_TRIGGER`
  行改写「部署级全局」措辞。
- `AGENTS.md` / `CLAUDE.md`「Agent 库理解」条若提及「经验库全局」需同步（实现时核对）。
- 代码内契约文本：`ports.py` `RetrievalExperienceStorePort`/`project_run_row` docstring、
  两侧 `sharing_store.py` 注释、`merge_dbs.py` 注释、`schema_manifest.py`、shadow manifest
  表说明、`retrieval_experience_job.py` 模块 docstring 与 `note_ask_completed` docstring。
- 守卫：隐私守卫改动见 §6；`test_architecture_documentation.py`、shadow 正向不变量、
  `notebook_delete_tables` 的登记测试按现有规则补。

## 11. 验收判据

1. `RETRIEVAL_EXPERIENCE_ENABLED=false`：两条链路零查询、零计数；注入侧逐字与接入前相同。
2. 同一库连续 10 次 reasoning 完成提问 → 发出一批 `partition="notebook"` 蒸馏；落库行
   `notebook_id` 为该库；另一库 0 行。
3. 注入闸开：库 A 的 run 送达块里本库条目排在全局条目之前；库 B 的 run 看不到 A 的任何
   条目（隔离用例：A 有条目、B 只有全局）。
4. consult_memory / 步级提示在本库分区上能取到条目，且差集记账只按送达行。
5. 删除库 A 后其分区行为 0，全局行数不变；深拷贝 A 得到的副本分区为 0 行。
6. `merge_dbs.py` 两库并集后各分区不超上限，全局分区 id 与合并前逐字相同。
7. 隐私守卫：原七条 + 判据八/九全绿；变异「job 模块读 `row["notebook_id"]`」被判据二白名单
   规则抓住；变异「把 notebook id 拼进 prompt」被判据九抓住。
8. 既有 `test_retrieval_experience_job.py` 除签名适配外不改断言。
9. 成本：每个库每 10 次 reasoning 提问 ≤1 次蒸馏调用，全局每 40 次 ≤1 次；两批不并发。

## 12. 刻意偏离与登记

1. **推翻 P2「触发器不收 notebook 参数」的明文登记**。P2 的论证是「知道是谁的 ask 的触发器
   离记录它只差一次重构」；本文正是要它记录到库粒度（不是人粒度），因此改由守卫（§6 白名单
   + 判据八/九）钉住「只到库、不到人、不进 prompt」，而不是靠签名不收参数。
2. **推翻 §12-Q3「全局库 + notebook 特征加权」**。Q3 的两条理由：per-corpus 无文献验证、
   冷启动全空。前者本来就是「机制形状可抄、作用域切法无前人验证」，产品目标明确要 by
   notebook 时不构成否决；后者由保留全局分区回退解决。原设想的「按 notebook 特征加权」
   （§10.1 偏离④ 登记未采集语料特征）随之不再需要，登记为撤销。
3. **单库计数仍是进程内的**，与今天同款接受（重启最多少蒸一轮，不影响正确性）。持久游标
   表待 §13-Q4。
4. **全局分区 id 哈希输入保持不变**（不加空 `notebook_id` 键），代价是 `experience_id`
   有两条分支；收益是零重算迁移、`merge_dbs` 对旧库逐字兼容。
5. **不做「本库条目晋升为全局」**：全局分区继续由全局链路独立蒸馏。两条链路各看各的样本，
   是两次独立的统计结论，不是同一结论的两份拷贝。

## 13. 拍板记录（2026-09-22，用户：「Q1–Q5 都按你推荐的来」）

- **Q1 守卫改法（已拍板）**：按 §6——保留禁键 + job 模块形参白名单 + 判据八/九。不采用把
  参数改名为 `partition` 绕过禁键表的做法：名字诚实，守卫改动也更能说清楚放行了什么。
- **Q2 界面条目列表（已拍板，PR-2 实施）**：给全体成员看条目（理由一句 + 动作 + 好坏 +
  支持数）。条目无主题、无来源，「越用越好」需要能被看见才能被信任与纠错；登记代价：成员能
  看到「这个库里别人问过哪种形状的问题」这一层聚合信息。
- **Q3 注入闸默认值（已拍板）**：PR-1 不动默认 false；落地后先在本机用 deepseek-flash 对
  提问最多的两个库跑出条目、开闸看 A/B（`fangan_todo.md:120` 那条），再单独 PR 翻默认。
- **Q4 计数持久化（已拍板）**：v1 进程内 + 手动「立即整理」按钮（PR-2）；若生产观察到重启
  频繁到攒不满阈值，再加一张 `(notebook_id, pending, last_distilled_at)` 游标表。
- **Q5 数值（已拍板，阈值经用户改定）**：单库阈值 **10**、单库上限 100、LRU 64 分区。

## 14. 分期建议

- **PR-1 后端**：迁移 + store 分区读写/淘汰 + 取数谓词 + job 双链路 + 注入合并读取 + 守卫
  + 删除/深拷贝/合库 + 四份文档。可独立合入，注入闸不动。
- **PR-2 界面**：P1 面板小节 + `distill_now` 端点 + 清空。
- **PR-3（拍板后）**：本机 A/B 结果 → 翻注入闸默认值，或按结果调 §9 数值。
