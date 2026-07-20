# batch_ingest 模型并发独立控制设计

日期：2026-07-20

## 1. 背景

`batch_ingest reparse` 当前把同一个 `conc` 同时用于两件事：

- `repo.settings.embed_concurrency = conc`：单个 source 内 element/chunk embedding 的 batch 线程数；
- `kg.scheduler.configure(job_workers=conc)`：同时运行的 source 流水线数。

每个 `process_source` 又会启动自己的 embedding 后台线程和 batch 线程池。因此将
`--embed-conc` 从 8 调大以增加在途 source、提高传统 LLM 利用率时，会同步放大 embedding
压力；多个 source 叠加后的峰值接近 `source jobs × per-source embed concurrency`。用户无法
在保持 embedding 服务低并发的同时独立拉高传统 LLM 吞吐。

CLI 还有两处相关不一致：

- `--embed-conc` 的 argparse 默认值固定为 4，即使用户未传，也会覆盖
  `.env` 中的 `EMBED_CONCURRENCY`；
- `kg --limit` 逐 source 串行执行，不能通过 source 并发填满传统 LLM。

## 2. 目标与非目标

### 2.1 目标

1. 提供三个互相独立、含义稳定的并发旋钮：
   - `--workers`：source 流水线并发；
   - `--llm-conc`：本次 batch 运行中传统 LLM 请求的全局硬上限；
   - `--embed-conc`：本次 batch 运行中 embedding 请求的全局硬上限。
2. 全局硬上限覆盖 `batch_ingest` 中所有相关阶段，而不是仅修复 `reparse`。
3. CLI 显式值优先；未显式传入时继承对应环境配置。
4. 提高 source/LLM 并发时，embedding 并发不再乘法放大。
5. 提供真实的 active/max/waiting 可观测数据。
6. 保持普通 FastAPI 服务的并发行为不变；限制器只在 batch 命令的作用域内启用。

### 2.2 非目标

- 不修改模型供应商、重试次数、退避算法、embedding batch 大小或 KG 窗口划分。
- 不把模型请求改造成跨进程分布式队列。
- 不改变 `vectors-to-blob` 的 CPU 进程池语义。
- 不为没有模型调用的 phase 人为创建线程池或限流器工作。

## 3. CLI 契约

### 3.1 参数

| 参数 | 稳定含义 | CLI 省略时 |
|---|---|---|
| `--workers` | 同时运行的 source 流水线数 | `KG_JOB_CONCURRENCY` |
| `--llm-conc` | 传统 LLM 逻辑请求的进程内全局硬上限 | `KG_EXTRACT_WORKERS` |
| `--embed-conc` | embedding API batch 的进程内全局硬上限 | `EMBED_CONCURRENCY` |

例：

```bash
PYTHONPATH=backend python scripts/batch_ingest.py reparse \
  --notebook-id nb-xxxx \
  --workers 32 \
  --llm-conc 24 \
  --embed-conc 4 \
  --pool-report-interval 5
```

其含义是最多同时运行 32 条 source 流水线、最多 24 个传统 LLM 逻辑请求、最多 4 个
embedding API batch。三者互不推导，也不相乘。

`vectors-to-blob` 是明确例外：其 `--workers` 继续表示 JSON 解析/重编码的进程数，省略时
继续使用现有 CPU 感知默认值；`--llm-conc` 和 `--embed-conc` 对该 phase 无实际作用。
`backfill-source-index` 同样没有模型调用。

### 3.2 优先级和校验

argparse 对三个模型/流水线参数使用 `None` 表示“未显式传入”。创建一次 `Settings` 后按
以下优先级解析有效值：

```text
显式 CLI > 对应 Settings（由 .env/环境变量解析）> Settings 字段默认值
```

所有有效值必须是正整数。`0` 和负数在 repository、scheduler 或线程池创建前直接报错并以
CLI 错误码退出。

启动时打印最终值和来源：

```text
concurrency: source=32(cli) llm=24(cli) embedding=4(cli)
```

环境继承示例使用 `env` 标记。

## 4. 架构

### 4.1 BatchModelConcurrencyController

在 batch composition root 新建一个作用域控制器。每次 CLI 执行只创建一个 controller，
持有两个共享 gate：

- LLM gate，容量为有效 `llm_conc`；
- embedding gate，容量为有效 `embed_conc`。

gate 至少暴露：

- `acquire`/`release` 或等价的异常安全上下文接口；
- `active`、`maximum`、`waiting` 快照；
- 不可变的正整数容量。

controller 进入时：

1. 保存 repository settings、模型 client/adapter 和 KG scheduler 的原配置；
2. 设置本次 batch 的 `kg_job_concurrency`、`kg_extract_workers` 和
   `embed_concurrency` 有效值；
3. 为 RuntimeModelProvider 安装共享 LLM gate；
4. 为 SourceEmbeddingService 安装共享 embedding gate；
5. 将 KG scheduler 的 job pool 配为 `workers`，window pool 配为 `llm_conc`。

controller 退出时，无论正常返回还是抛异常，都恢复原配置。CLI 进程通常随即退出，但恢复
仍是程序化调用和测试隔离的必要契约。

### 4.2 LLM 调用边界

RuntimeModelProvider 在 batch gate 启用时，为它解析出的所有传统 LLM client 返回共享
限流代理。代理保留底层 client 的 `configured`、`model`、`settings` 等鸭子类型属性，仅在
`chat_json` 周围取得和释放同一个 gate。

这保证下列由 batch 触发的调用共享同一个硬上限：

- source 摘要；
- notebook 元数据自动补全；
- 论文元数据；
- KG 窗口抽取、refine、gleaning；
- conflict/merge review；
- unified rebuild 中的概念描述；
- 未来在同一 batch 调用链中经 RuntimeModelProvider 解析出的其他传统 LLM `chat_json`。

当主 LLM 与 KG LLM 实际指向同一个底层 client 时，代理缓存不得造成双重 acquire；每个
最外层逻辑 `chat_json` 调用只占一个槽。

一次 `chat_json` 的内部重试和退避期间继续持有许可。这使参数成为“在途逻辑请求”硬上限，
在供应商开始 429 时不会由其他等待任务立即补位形成持续冲击。

### 4.3 Embedding 调用边界

SourceEmbeddingService 的所有 batch 计算路径共享同一个 embedding gate，包括：

- element embeddings；
- chunk embeddings；
- knowledge object embeddings；
- relation embeddings；
- 单对象/查询形式、但属于 batch ingestion 写入路径的 embedding。

许可粒度是一个实际送往 embedder 的 batch，而不是一个 source。每个 batch 在调用
embedder 前取得许可，在成功或异常返回后释放；向量持久化不占用许可。

不能简单保留“每 source 创建 `embed_conc` 个任务，然后在 client 内阻塞”的实现，否则
`workers × embed_conc` 个线程仍可能被创建。batch 提交必须有界：只有取得共享许可的 batch
才能进入实际执行任务，等待者停留在有界的上层 source/job 线程中。由此：

- 实际 embedding HTTP 并发不超过 `embed_conc`；
- embedding worker 数不随 `workers × embed_conc` 乘法增长；
- 单个大型 source 在其他 source 空闲时仍可使用最多 `embed_conc` 个槽。

### 4.4 SQLite 与 ContextVar

任何 gate 等待和模型网络请求都不得持有 SQLite 写事务。现有顺序继续保持为“读取输入 →
模型计算 → 短写事务持久化”。

KG scheduler 已有的 `copy_context` 传播必须保留。LLM 代理在 client 解析完成后工作，不得
重新解析或丢失请求用户 ContextVar，确保 batch `--owner` 选择的用户模型配置仍然生效。

## 5. 各 phase 数据流

### 5.1 `reparse`

- targets 仍仅包含缺少 `source_elements` 的 sources；
- job pool 使用 `workers`；
- 每个 source 的 parse、摘要、元数据、KG 抽取保持现有顺序；
- LLM 调用经过全局 LLM gate；
- 后台 element/chunk embedding 经过全局 embedding gate；
- 不再用 `embed_conc` 配置 job pool；
- 收尾 rebuild 和节点 embedding 继续使用相同两个 gate。

### 5.2 `all`

- source job pool 使用 `workers`；
- KG window pool 使用 `llm_conc`；
- 所有 source 后台 embedding 共享全局 `embed_conc`；
- 删除文档中“峰值约为 `workers × embed_conc`”的旧契约，峰值改为硬上限
  `embed_conc`。

### 5.3 `kg`

- 无 `--limit` 时，现有 `build_notebook_kg` 跨 source job 使用 `workers`；
- 有 `--limit` 时也把选中的 targets 提交到 source job pool，不再逐 source 串行；
- 所有 KG window 调用受 `llm_conc` 限制；
- rebuild 中独立的 `kg-desc`、`kg-review` 池可以并行提交，但实际 `chat_json` 仍受同一
  LLM gate 限制；
- 节点 embedding 受全局 embedding gate 限制。

### 5.4 `metadata`

- source 工作池使用 `workers`；
- 移除当前内部固定 `min(8, kg_extract_workers)` 的并发决定；
- 每个论文元数据调用仍受全局 LLM gate 限制。

### 5.5 `ingest`

- 文件/source 处理使用 `workers`；
- source 摘要等实际发生的传统 LLM 调用受 LLM gate 限制；
- 解析期现有的 embedding 暂停策略不变；
- 收尾 chunk embedding 受全局 embedding gate 限制。

### 5.6 `embed`

chunk 和 node 缺失向量回填共享 embedding gate。该 phase 不使用 LLM gate。

### 5.7 无模型 phase

`index`、`vectors-to-blob`、`backfill-source-index` 不新增模型工作。若未来 `index` 出现
真实 embedding 请求，它必须自动走同一 embedding 边界，而不能绕过 controller。

## 6. 可观测性

`--pool-report-interval` 改为报告 gate 真值，而不是仅根据线程名前缀估算模型并发。推荐
输出：

```text
[pool 17:52:33] LLM 23/24 waiting=5 · embedding 4/4 waiting=18 · source 29/32 · 源完成 5/40
```

结构化 manifest 事件同步记录：

- `llm_active`、`llm_max`、`llm_waiting`；
- `embed_active`、`embed_max`、`embed_waiting`；
- `job_active`、`job_max`；
- phase、done、total。

线程名计数可以保留为诊断附加项，但不再作为并发上限是否生效的权威数据。

## 7. 错误处理

- 所有 permit 必须在 `finally` 中释放。
- 模型超时、429、解析错误或持久化错误不得永久减少 gate 容量。
- 单 source 失败继续按现有策略隔离；其他 source 可以继续获取许可。
- controller 安装失败时不得开始 phase；退出恢复失败应记录清晰错误，但不得掩盖原始
  phase 异常。
- pool reporter 继续 fail-open，观测失败不能中断 ingestion。

## 8. 兼容性与文档

- CLI 新增 `--llm-conc`。
- `--embed-conc` 的含义从“单 source 线程池大小”收紧为“本次 batch 的全局硬上限”。
- `--workers` 在 `reparse`、`kg --limit`、`metadata` 中获得与其名称一致的 source
  并发语义。
- 不传 CLI 参数时改为尊重 `.env`；这是有意修复，不保留 argparse 固定 4 覆盖
  `EMBED_CONCURRENCY` 的旧行为。
- 同步更新 `README.md`、`README_zh.md`、`AGENTS.md`。不把本变更记为
  `silicon_notebook_fangan.md` 中尚未完成的产品功能，因此不新增虚假的
  `fangan_done.md` 完成项。

## 9. 测试与验收

### 9.1 单元测试

1. gate 在并发任务下从不超过 maximum，并正确统计 active/waiting。
2. 正常返回和异常返回均释放 permit。
3. LLM 代理保留鸭子类型属性、ContextVar 和返回值/异常。
4. 同一底层 client 被多个角色引用时不会对一次调用双重 acquire。
5. embedding 有界提交不会创建 `workers × embed_conc` 个执行线程。
6. controller 正常和异常退出后均恢复 settings、client 和 scheduler。

### 9.2 CLI/服务测试

1. 显式 CLI 值覆盖环境配置。
2. 省略参数时分别继承 `KG_JOB_CONCURRENCY`、`KG_EXTRACT_WORKERS`、
   `EMBED_CONCURRENCY`。
3. 三个参数为 0 或负数时在工作开始前失败。
4. 多 source `reparse` 中 embedding 峰值严格不超过 `embed_conc`。
5. 同一场景证明 LLM 峰值可以高于 embedding，例如 LLM 达到 8、embedding 不超过 2。
6. `all`、`kg`、`reparse`、`metadata`、`ingest`、`embed` 接入正确 gate。
7. `kg --limit` 的 source 峰值可达到 `workers`，同时 LLM 峰值仍不超过
   `llm_conc`。
8. pool reporter 输出和结构化事件反映 gate 真值。

### 9.3 完成门槛

- 相关后端测试通过；
- `scripts/check.sh` 通过；
- `cd frontend && npm run build` 通过；
- 中英文 README 与 AGENTS 并发契约一致；
- 使用如下实测配置时，日志能证明 LLM 与 embedding 独立受控：

```text
source=32, llm=24, embedding=4
```

验收重点不是线程池配置值，而是观测到的模型请求峰值分别满足：

```text
llm_peak <= 24
embed_peak <= 4
```
