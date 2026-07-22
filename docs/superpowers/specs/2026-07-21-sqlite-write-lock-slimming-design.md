# SQLite 写锁瘦身设计

日期：2026-07-21
状态：已获用户批准，等待实现计划

## 1. 背景

部署环境出现两个症状：往一个 notebook 写 KG 时，另一处写同一个 SQLite 库会报
`database is locked`；同时网页卡住。用户已定位到当时有两个进程在写同一个库（服务端
进程与离线 `batch_ingest` 进程）。

顺着这条线讨论出的方向是「把 `batch_ingest` 的补抽能力搬进在线，让日常运维不再需要
开第二个进程」。那份设计是本文的**姊妹文档**
`docs/superpowers/specs/2026-07-21-notebook-kg-repair-design.md`（分支
`codex/notebook-kg-repair-design`）。

但那个方向只解决「为什么要开第二个进程」，不解决「开了会怎样」。对照代码后确认：即使
收敛到单进程，**页面卡住的那一半症状不会消失**，因为进程内的写锁本身就会被若干长写
事务长期占住。本文只做这一件事。

## 2. 根因

`SqliteDatabase.write()`（`backend/app/repositories/sqlite/database.py:104`）是
「进程级 `threading.RLock` + 一条独立连接」，`with` 块整体是一个 SQLite 写事务：

- **同进程**第二个写者排在 `write_lock` 上 —— 页面转圈，不报错；
- **跨进程**第二个写者等 `busy_timeout`（`DB_BUSY_TIMEOUT_MS`，默认 30000）后抛
  `database is locked` —— 报错。

两个症状同源：**只要有一个写事务持续足够久**。SQLite 的写锁是整库级的，与 notebook
无关，所以「不同 notebook 各写各的」不提供任何隔离。

### 2.1 长写事务清单

| # | 位置 | 行为 | 为什么长 | 拆开的语义风险 |
|---|---|---|---|---|
| 1 | `services/knowledge_lifecycle.py:1503` | 把 object→seed 暂存进 `kg_cluster_scratch` | 整个全库扫描 + 每对象的 Python 计算（`_fast_loads`、`seed_or_unique`、alias 匹配）全在写锁内，且边持锁边从另一条连接拉游标 | **零**：写的是 scratch 表，无任何读者 |
| 2 | `services/knowledge_lifecycle.py:1586` → `repositories/sqlite/unified_kg_store.py:76` | `DELETE FROM concept_clusters` + 流式 INSERT 全部成员行 | 同样持锁拉游标 + Python 逐行造 tuple | **高**：读者会看到半个表（`cluster_map` 一半新一半旧 → 串簇/漏召回） |
| 3 | `repositories/sqlite/unified_kg_store.py:502` | `replace_canonical_relations` | `DELETE` + 全量 `executemany` | 中 |
| 4 | `repositories/sqlite/unified_kg_store.py:529` | `replace_mention_bridge`（`mention_edges` + `concept_comentions`） | 同上 | 中 |

已核查**不在**名单内的路径：向量 backfill（`repositories/sqlite/maintenance.py:651`、
`:687`）已是 `LIMIT batch_size` 的逐批独立提交；scale-index 构建写的是文件系统产物。

### 2.2 只把事务改短还不够

`write_lock` 是 `threading.RLock`，**不保证公平**：`release()` 只唤醒等待者，等待者仍要
和释放后立刻回来重新 `acquire()` 的持有者抢同一把互斥量，而后者此刻正在 CPU 上跑，几乎
每次都赢。一个批量写者 `for batch: with write(): ...` 连续抢锁时，交互写可能长时间拿
不到锁。因此只把单次事务改短还不够，还要解决锁本身的不公平——最终方案是换掉锁本身
（`threading.Lock`，见 §5.4），不是在批量写循环里插入显式的 sleep 让路。

## 3. 目标与非目标

### 3.1 目标

1. 批量写（重聚类、关系层、mention 桥、backfill）运行期间，交互写不出现可感卡顿。
2. 「不卡住」由**实测指标**验收，不由「事务看起来变短了」验收。
3. 不引入新的正确性风险：整表替换对读者仍然原子。
4. 改造点按实测数据推进，不为推演出来的瓶颈付代价。
5. 收益覆盖所有共用路径（看板「重新合并知识图谱」、离线 CLI、base 库重建），不只服务
   姊妹文档里的 repair 任务。

### 3.2 非目标

- 不做跨进程协调（整库级维护租约属于姊妹文档，见 §9）。
- 不迁移到 PostgreSQL，不引入外部队列。
- 不改变聚类、关系层、mention 桥的**算法语义**；本文只改写入的事务切分与调度。
- 不改 `concept_clusters` 的读者。generation 列方案是逃生口（§5.6），默认不做。
- 不改 Ask、报告、上传、MinerU 等路径的重试与并发策略。

## 4. 选定方案

四步，顺序不可交换：

1. **先量**：给 `write()` 加 wait/hold 仪器 + 离线聚合子命令。
2. **建基准**：合成一个可复现的大规模 notebook 基准，本地就能量到部署机量级。
3. **改**：必做项 1 处 + 新增 `bulk_write()` 原语；其余 3 处由基准数字决定是否做。
4. **验**：按 §6 的分档门槛验收，并做变异验证。

## 5. 详细设计

### 5.1 仪器

在 `SqliteDatabase.write()` 上分开采集两个量：

- `wait_ms`：从请求 `write_lock` 到拿到锁的耗时。**这是「页面卡住」的直接度量。**
- `hold_ms`：持锁时长。**这是「谁害的」。**

要点：

- **调用点标识**用 `sys._getframe()` 取 `filename:lineno`，微秒级，相对一次 DB 写可
  忽略。不使用 `traceback.extract_stack`。
- **重入**：`write()` 允许同线程嵌套调用，嵌套写深度用 thread-local 计数处理（最终
  实现见 §5.4：深度记账搬到了锁外）。仪器**只测最外层**，避免内层重复计数把 hold
  时长算重。
- **动态读锁对象**：`sqlite_repository.py:950` 存在 `_write_lock` 的 setter（测试会替
  换锁对象），仪器必须每次读 `self.write_lock`，不得在 `__init__` 缓存。
- **等待者计数**：仪器维护一个进程级 `waiters` 计数（请求锁前 +1、拿到后 −1），仅作
  观测量（`stats.waiters`，供测试与诊断直接读取）。§5.4 最终选定的公平性方案不消费
  这个计数——`bulk_write()` 没有让路分支，锁本身的交接语义对所有写者生效，与是否统计
  等待者无关。
- **输出**：进程内按调用点聚合直方图；**单次超硬阈值**立即发一条事件，**聚合快照**按
  固定间隔发一条。两者都进 `events.jsonl`（`kind` 取 `db_write_lock` 系列），保持
  仓库既有的「调试先看 events.jsonl」路径。不做逐次调用
  发事件，否则日志会被淹没。
- **开关**：默认开启（开销是两次 `perf_counter` + 一次 `_getframe`）。可用环境变量关
  闭。环境变量映射必须用 `validation_alias`（`pydantic-settings` v2，`Field(env=)` 无效）。

新增 `scripts/diag.py locks` 子命令：从 `events.jsonl` 离线聚合出每调用点的
wait/hold 分布（P50/P95/P99/max）。它必须遵守 `diag.py` 既有约束 —— **纯 stdlib、零
`app` 依赖、只读、脱敏**，与 `slow` / `latency` 同级。按仓库惯例，新增的用户可运行 CLI 必须在同一个 PR 内写进
`README.md` 与 `README_zh.md`。

### 5.2 合成基准

开发机上的库通常只有 10^4–10^5 量级的 `knowledge_objects`，量不出部署环境（基础库
10^5–10^6 量级）的分布。因此配一个规模可调的合成基准：

- 造一个 N 规模 notebook（N 可调，默认跑到 50 万对象量级），只造聚类输入所需的最小数
  据，**不调用任何模型**；
- 跑 `rebuild_unified_kg`，输出 §5.1 的各调用点 wait/hold 分布；
- 同时作为回归守卫进测试（大规模档默认跳过，小规模档常跑）。

它是本设计的**唯一数据来源**：§5.5 做不做、§6 是否达标，都以它为准，不以代码审查的
直觉为准。

### 5.3 改造点 1（必做，零语义风险）

`knowledge_lifecycle.py:1503` 的 scratch 暂存改成分批提交：写的是
`kg_cluster_scratch`，没有任何读者，当前却把全库扫描与每对象的 Python 计算整个圈在写锁
里。改为每批（沿用现有 1000 行的缓冲边界）独立提交，Python 计算移出写锁。

`run_id` 隔离已经存在（`stream_scratch_rows` 按 `run_id` 过滤），分批提交不会让并发
rebuild 互相看见对方的中间行。中断后残留的 scratch 行由现有
`clear_scratch_run` 在下次同 `run_id` 起始处清理。

已知限制（评审后补记）：分批提交把 Pass A2 的读游标（`self._connect()` 复用的线程本地
连接）变成了一个贯穿整个类型扫描期间不释放的步进游标——实测探测确认
`PRAGMA wal_checkpoint(TRUNCATE)` 在该游标未耗尽时返回 busy，游标耗尽后才成功。这与
改造前不同：改造前是未提交的写事务 + 持有的写锁在挡 checkpoint，现在变成同一条读连接
上的长活读快照在挡。这是一次以 IO 为动机的改动带来的另一个 IO 副作用（磁盘/WAL 文件
增长，而非正确性问题），记录留待后续决策，本次不处理。

### 5.4 新原语 `bulk_write()`

在 `SqliteDatabase` 上新增一个批量写原语 `bulk_write(batches, apply) -> int`：每批一个
独立短事务（`with self.write() as conn: apply(conn, batch)`），提交后立刻进入下一批。
**没有 `yield_seconds` 参数，批间不 sleep**——公平性完全由 `write_lock` 本身保证，不需要
`bulk_write()` 主动检测等待者、主动让路。

**本节按最终实现记录，与最初设计（本节上一版）已分叉；分叉的原因和数字如下。**

最初的设计是「每批提交后，若 §5.1 的 `waiters > 0` 就 `sleep(yield_seconds)` 让路」。这段
代码写出来过、也接入过基准，结论是**在两种受支持的配置下都是错的**，已删除：

- `DB_WRITE_LOCK_STATS` 开（默认）时 `stats` 非 `None`，sleep 分支可达——但此时仪器自身
  （`_caller_site()` 走栈 + `stats` 的两次加锁）恰好落在 release 与重新 acquire 之间，
  已经顺带充当了交互写的插队窗口，sleep 纯属多余；代价是有交互写活动时批量吞吐掉到约
  1/4（实测 4s 内 7375~8288 批 → 1578~2126 批），交互写延迟反而略升（中位
  0.4ms → 0.8ms）。
- `DB_WRITE_LOCK_STATS` 关（受支持的配置）时 `stats is None`，`waiters` 根本取不到，
  sleep 分支**永远不会执行**——而这恰恰是 RLock 饿死真正发作的配置。也就是说，公平性
  意外地绑在了一个只该影响观测的开关上：关掉它，正确性也跟着变了。

真正的根因不在批循环，在锁本身。`write_lock` 原是 `threading.RLock`——纯 barging 锁：
`release()` 只唤醒等待者，等待者仍要和 release 后立刻回来重新 `acquire()` 的持有者抢
同一把互斥量，而后者此刻正在 CPU 上跑，几乎每次都赢。实测（CPython 3.13.11，单等待者 +
紧循环持有者）：`threading.RLock` 让单个等待者最坏饿死过 **198.2 秒**；换成
`threading.Lock` 后，同一套压力下最坏等待**从未超过 9 毫秒**。差别不在 OS，在 CPython：
`threading.Lock` 底层是 `PyMutex`，实现了 eventual fairness——等待超过约 1ms 的 waiter
在 release 时被直接交接（handoff），不再参与抢。完整数字、推导与「为什么这份公平性是
CPython ≥3.13 的实现细节」，见 `backend/app/repositories/sqlite/database.py` 模块头
「进程级写锁说明」。

落地的修复分两层：

1. **换锁**：`write_lock` 改为普通 `threading.Lock`（不可重入）。
2. **重入挪出锁外**：同线程嵌套 `write()` 不再靠锁自己处理重入，改由 `write()` 的
   thread-local `write_depth` 记账——只有最外层（`depth == 0`）才 acquire/release，
   内层只加减深度、完全不碰锁。

中途还写过、又删掉了一版自定义可重入锁 `_FairWriteLock`（在 Python 层维护 owner/count
做重入）。删除的原因：owner/count 的写入跨越多条字节码边界，`KeyboardInterrupt` 这类
异步异常可能恰好落在「拿到底层锁」与「记好 owner」之间、或「清 owner」与「放底层锁」
之间——一旦命中，会留下"底层锁已放、`_owner` 仍指向某线程"的状态：该线程下次重入
`acquire()` 走"成功"的快路径却什么都没持有，同时另一线程也能拿到那把已经空闲的底层
锁，**两个线程同时进入写临界区**（静默并发写损坏）。这不是推演，该状态已被端到端复现。
`threading.Lock` 没有 owner 概念，acquire/release 各是一次 C 调用，没有 Python 层记账
可以失步——这一整类缺陷从结构上消失。

公平性的回归守卫（`tests/test_bulk_write_fairness.py`）是一条**确定性插队探针**，不是
毫秒阈值：在本仓库固定的 `-n 12` 并行度下，毫秒阈值被实测为**零区分力**——每组 15 次
重复，「正确实现」（`threading.Lock`）与「已知有饿死缺陷的实现」（`threading.RLock`）
全都是 0/15 报错，通过与否不携带任何关于代码对错的信息。插队探针不依赖墙钟阈值，断言
的是一个结构性布尔量（锁在有等待者驻留时，是被交接出去，还是被持有者抢回去），因此在
同样打满机器的负载下仍然有检出力。

残留窗口（如实记录，既不是本次改动引入，也没有被本次改动放大）：`lock.acquire()` 返回、
到进入负责 release 的 `try` 之间隔着一条字节码边界，异步异常可能恰好落在那里，让底层
锁停在"锁着"且再没有人会去放——**进程级写死锁**，但**绝不会**退化成两个写者并发写入。
纯 Python 关不掉这个窗口（需要 C 扩展或屏蔽信号）。接受它的理由：①它退化成可观测的
"卡住"（进程停住、栈一看就知道卡在哪），而不是静默的并发写损坏；②本应用里唯一现实的
异步异常来源是 SIGINT，收到 SIGINT 就意味着进程正在退出，此后不再有写者需要这把锁。

所有整表替换与 backfill 路径改走 `bulk_write()`。`write()` 语义不变，现有 233 处调用点
零改动。

仓库约束：`SqliteDatabase` 的成员受 `repositories/ownership_manifest.py` 管辖
（`write_lock`、`db_path` 等已登记），新增公开成员需要同步登记；SQL 仍必须收在
`repositories/sqlite` 层，服务层不得出现裸 SQL（`callers_static` 约束）。

### 5.5 改造点 2/3/4（门槛驱动）

`concept_clusters` / `canonical_relations` / `mention_edges`+`concept_comentions` 三处
整表替换，**只有在 §5.3 + §5.4 完成后基准仍不达标时才做**。做法统一为「预备段 + 切换
段」：

- **预备段**：结果先写进 scratch（无读者）→ 分批提交、可中断续跑、时长不受限；
- **切换段**：一个短事务，**纯 SQL** `DELETE ... WHERE notebook_id=? [AND object_type=?]`
  + `INSERT ... SELECT ... FROM <scratch>`。无 Python 往返、无跨连接游标。

原子性完整保留（读者要么看到旧的整表，要么看到新的整表），**读者零改动**。

代价：`concept_clusters` 的 seed→canonical 映射（含 canonical 名与描述）目前只存在于
Python dict，需要落表。这是一次 schema 变更 —— 按仓库的迁移约定**追加**一个
`_migration_N` 并 bump `SCHEMA_VERSION`（当前 22），**不得塞进已封版的既有
migration**（否则版本闸对已部署库会短路，`IF NOT EXISTS` 救不了，因为压根不会执行到）。姊妹文档的 `parse_outcome` 列若同期落地，合并成同一次迁移。

已知限制：切换段的 `DELETE` + `INSERT...SELECT` 在 60 万行量级预计仍在**秒级**，B 树
维护那部分省不掉。这是 §6 把切换段门槛定为 1s 而非 100ms 的原因。

### 5.5.1 判定结果（2026-07-21，实测）

依据：`backend/tests/test_write_lock_benchmark.py` 在 **500k 对象**档的实测，
完整表格与推导见
`docs/superpowers/plans/2026-07-21-sqlite-write-lock-slimming.md` Task 8。
§5.3 + §5.4 均已完成后重测，§6 四档门槛 **3 档未达标**。

| 改造点 | 目标 | 500k 实测 `hold_max` | 占重聚类墙钟 | 判定 |
|---|---|---|---|---|
| **2** | `concept_clusters` | **1240.8ms**（三跑 1240.8 / 1375.3 / 1460.2） | **19.7%** | **做** |
| **3** | `canonical_relations` | 35.3ms | 0.16% | **不做** |
| **4** | `mention_edges` + `concept_comentions` | 323.6ms | 1.5% | **做**（次序在 2 之后） |

- **改造点 2 —— 做。** 它是唯一一个自己就打破所属门槛的点（1240.8ms > 1s，超 24%），
  且吃掉重聚类期间**全部持锁时间的 73%**（4283ms / 5858ms）。300k→500k 的标度是
  ~N^1.42，外推到部署上限 10^6 约 **3.7s**。
- **改造点 3 —— 不做。** 35.3ms，比它所属的 1s 门槛低一个半数量级，比 50ms 常态写
  门槛也还低；标度近似线性，10^6 时约 70ms。为它付一次 schema 迁移不划算。
- **改造点 4 —— 做，但排在 2 之后；这是本判定里唯一一条边际结论。**
  它**通过**了 1s 切换段门槛（323.6ms，3 倍余量），按本文原判定规则的字面本该
  「不做」。改判的理由是另一条门槛：写锁是交接锁（§5.4，`threading.Lock`，由
  `test_bulk_write_fairness.py` 的确定性插队探针钉住），交互写的等待 ≈ 它到达
  瞬间那次写的剩余持锁时长，所以「等待超过 w」的到达时间质量
  = `Σ_i max(0, d_i − w)`，P99 就是让它等于 `1% × 重聚类墙钟`（500k 档 = 218ms）
  的那个 w。据此逐级推：

  | 状态 | 交互写 P99 | 达标? |
  |---|---|---|
  | 现在 | ≈1.0~1.2s | ❌ |
  | 只做改造点 2 | **≈106ms** | ❌（**只超 6%**） |
  | 改造点 2 + 4 | ≈18~30ms | ✅ |

  **诚实的话：在 500k 这一档，改造点 4 的收益是「把 106ms 压到 30ms」，即它要不要做
  完全押在那 6% 的越界上 —— 这个 margin 远小于下文列出的建模失真。** 支持仍然做它的
  是规模：`:2241` 的标度约 N^1.30，10^6 时约 **0.8s**；即使重聚类墙钟同步翻倍
  （预算 ~440ms），P99 仍≈360ms，三倍超标，且届时它自己也逼近 1s 切换段门槛。
  因此结论是「做，但可以在改造点 2 落地后按真实规模复测再最终确认」——如果复测显示
  `:2241` 在生产规模下仍显著低于预期，它可以推迟；改造点 2 没有这个可选性。

**⚠ 必须同时记下的一条：§6 的第 1 行与第 4 行在交接锁下互相矛盾，改造点 2 本身
能不能兑现取决于它把切换段压到多少。** 「交互写 wait P99 < 100ms」的充要条件是
`Σ_i max(0, d_i − 100ms) < 1% × 重聚类墙钟`（500k 档 = 218ms）。代进 4 次切换：

| 改造点 2 之后每次切换的 `hold` | 交互写 P99 |
|---|---|
| ≤ **154ms**（需 ~8.9 倍改善） | < 100ms ✅ |
| 500ms | ≈446ms ❌ |
| 1s（= §6 第 4 行允许的上限） | ≈945ms ❌ |

即 **§6 第 4 行允许的 1s，代进第 1 行必然得到约 0.95s 的 P99** —— 两行不可能同时
成立。而本文 §5.5 自己预测：改造后的切换段在 60 万行量级**仍是秒级**。所以改造点 2
落地后必须实测切换段究竟落在哪一档，而不能默认它自动兑现第 1 行。

因此二者必居其一，需要用户拍板（本判定不替用户选）：

1. **收窄第 1 行的口径**：承认「每 notebook×object_type 一次的秒级切换」是可接受的
   偶发延迟，把 wait P99 门槛限定在**切换段之外**的窗口。这与 §10 决策 3 的精神
   一致——但要注意 §10 决策 3 用的理由是「次数少」，而在交接锁下决定 wait 分位数的
   是**时间占比**（19.7%），不是次数（4 次）；这条理由需要按时间占比重新表述。
2. **启用 §5.6 的逃生口**（`concept_clusters` 加 generation 列），把切换退化成更新
   一个状态行。代价是所有读者按 generation 过滤，工作量大一个量级。

**另外两处超门槛、且不属于任何既有改造点的写**（Task 8 新发现，详见计划 Task 8）：

- `knowledge_lifecycle.py:1482` / `:2040`（`clear_scratch_run`，入口与 `finally` 各一处）
  ——500k 档 **87.0ms / 70.9ms**（`:1482` 四次运行 79.9~122.5ms，是本表抖动最大的一处），
  双双超 §6 的 50ms `hold` 门槛。`:2040` 是 §5.3 的**副产物**：分批提交换掉了
  「中断即原子回滚」，所以必须新增一处无条件的 `finally` 清理——**这处写在改造点 1
  之前并不存在**。做完 2 和 4 之后，它们就是持锁最长的两处；但注意它们**不会**把
  交互写 P99 顶过 100ms（那时 P99 ≈ 18~30ms，因为这两处的到达时间占比只有 1.1% +
  0.33%）。也就是说：它们破的是 §6 第 2 行的 `hold` 门槛，不是第 1 行的 wait 门槛，
  优先级低于改造点 2/4。
- **§6 第 2 行「常态写」在本基准里根本没有被覆盖**：`measure_rebuild` 在重聚类前
  `stats.reset()`，整个摄取路径被丢弃。补测一次种子阶段，`knowledge_lifecycle.py:253`
  （`store_kg` 的对象落库，正是 §6 点名的 per-source 提交）是 **max 334.8ms /
  均值 171.3ms**。⚠ 基准一次 `store_kg` 塞 2000 个对象，真实单来源远小于此，所以这
  **不是**对该门槛的判决——只说明这一行至今无有效证据，且看起来有风险。要判它得另
  设一个按真实来源规模切批的基准。

### 5.6 逃生口（默认不做）

若基准显示切换段无法压进 1s 且用户判定不可接受，唯一的下一步是给 `concept_clusters`
加 generation 列：新行写新 generation，切换退化为更新一个状态行，旧行由后台分批懒清
理。代价是**所有读 `concept_clusters` 的地方都要按当前 generation 过滤**，工作量大一个
量级。本设计不做，仅记录为逃生口。

## 6. 验收门槛

**唯一门槛（面向用户，2026-07-22 定稿）：**

| 判据 | 门槛 |
|---|---|
| 交互写 `wait_ms`（有批量任务并发运行时实测） | **P99 < 100ms 且 max < 500ms** |

由 §5.2 的基准 + `tests/test_bulk_write_fairness.py` 实测，不由代码审查判定。

### 6.1 为什么只留一条

初版把四个内部分段各自定了门槛（交互写 wait、常态写 hold、批量预备段 hold、原子切换段
hold < 1s）。2026-07-21 的 500k 实测暴露出这套分档**自相矛盾**：写锁是交接锁
（`threading.Lock`，见 §5.4），一个等待者最坏就等一个持有者，所以**切换段持锁多久，交互
写就等多久**。允许切换段 1s，等于允许交互写 P99 接近 1s —— 与第一行的 100ms 直接打架。

矛盾的根源是把**手段**当成了**目的**：内部分段各花多久是实现细节，用户能感知的只有一个
量 —— 提交一次写要等多久。因此四行收敛成一行，其余 per-site `hold_ms` 降级为**诊断
指标**（用 `scripts/diag.py locks` 看谁是大头），不再是独立门槛。

刻意**没有**选的两个替代方案，以及为什么：

- **收窄口径**（把「每次重聚类一次的切换段」排除出 P99 统计）—— 等于把用户的原始抱怨
  定义掉。本设计已经删过一条这样的守卫（§5.4 里那条 0/15 检出率的毫秒断言），不再重蹈。
- **把切换段门槛直接收到 100ms** —— 那等于凭推演提前承诺 §5.6 的 generation 列（要改遍
  所有 `concept_clusters` 读者，工作量大一个量级）。本设计的推演已经被实测推翻过三次
  （§5.4 的让路机制、§5.1 的归因栈深、毫秒阈值的可测性），不再凭推演承诺大改造。

### 6.2 由此确定的推进顺序

1. 做改造点 2（§5.5），目标是把切换段压进 **100ms 量级**；
2. 按本节这一条判据实测；
3. **只有实测仍不达标**，才启用 §5.6 的 generation 列。

改造点 3 已由 500k 实测出局（35.3ms，两个数量级余量），见 §5.5.1。

### 6.3 一条已知的覆盖缺口

常态写（per-source 提交、上传、Ask 落库）的 `hold_ms` 在现有基准里**未被覆盖** ——
`measure_rebuild` 在重聚类前 `stats.reset()`，摄取路径整段被丢弃。它现在不是门槛，但
「摄取路径会不会自己就顶到 100ms」这个问题仍然没有答案，需要单独的并发摄取基准。

## 7. 变异验证

加了守卫不等于守卫有效，必须把改造点改回
「一个大事务」的形态，确认基准/测试**真的变红**。

具体要求：

- 变异前先 `grep -c` 确认改到的是真正被执行的代码，而不是同名常量或已漂移的行号；
- 管道后的 `$?` 是管道末端的退出码，不是守卫的 —— 断言退出码要独立取；
- 除「删除」变异外，还要做「移动」变异（把批提交移出循环），确认守卫不是靠
  `[\s\S]*?` 越过块尾大括号误判通过。

## 8. 仓库验收

- `scripts/check.sh` 全绿；
- `cd frontend && npm run build` 通过（本设计不改前端，此项作为回归）；
- 若改动触及 `README.md` / `README_zh.md` / `AGENTS.md` / `architecture.md` 的措辞，同步
  `backend/tests/test_architecture_documentation.py`；
- 若测试文件增删导致行号移动，重跑 `test_repository_surface_manifest` 依赖的行号基线；
- 新增 `diag.py locks` 子命令写进两份 README。

## 9. 与姊妹文档（在线 repair）的关系

本设计是 `2026-07-21-notebook-kg-repair-design.md` 的**前置条件**：repair 的甲3 终点
（reparse → 补抽 → 融合 → 补向量 → 索引）必然走到 §2.1 的第 1/2 处，没有本设计
repair 一定会卡住所有人的页面。

讨论中同时确认了三条**必须回填到姊妹文档**的决定（不在本 PR 范围内）：

1. **定位收敛为单进程**：`batch_ingest` 降级为「服务停机时的一次性大批量导入」，服务
   运行期间不再跑。
2. **§6.1 的 interactive/repair 双 lane 公平调度删除**：单进程下 `write_lock` 天然串行，
   不会报错，那套复杂度不再需要。有界投递（不为几万源预建 future）保留。
3. **§6.3 的维护租约改为整库级**，不是 per-notebook —— SQLite 写锁是整库级的，
   per-notebook 租约在「我写 A、别人写 B」这个原始场景下两边都能拿到租约。租约的作用
   是防止有人手滑在服务运行时跑 `batch_ingest` / `merge_dbs.py` / `recluster_kg.py`，
   它是「单写者」这个前提唯一的执行机制。

## 10. 已确认决策

1. 先做写锁瘦身独立 PR，再做 repair PR。理由：`rebuild_unified_kg` 是共用函数，今天点
   「重新合并知识图谱」就会卡，瘦身独立有价值；且 repair 的正确性评审与写锁的性能评审
   是两个维度，混在一个 PR 里两边都看不清。
2. 先量再改。改造点 2/3/4 由基准数字决定，不预先承诺。
3. 原子切换段门槛定 1s（而非 100ms）。每次重聚类每种 object_type 各一次，偶发秒级写延迟
   不构成「页面卡住」。
4. 不改 `concept_clusters` 的读者。generation 列是逃生口，不是默认方案。
5. 仪器与基准走既有结构：事件进 `events.jsonl`，聚合进 `scripts/diag.py`，不新起脚本。
