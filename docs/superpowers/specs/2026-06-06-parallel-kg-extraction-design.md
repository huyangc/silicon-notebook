# 全局并行 KG 抽取调度（设计）

日期：2026-06-06
分支：`feat/parallel-kg-extraction`（基于 origin/master `d32e6c7`）
关联：`2026-06-05-adaptive-extraction-windows`（自适应窗口）

## 背景与问题

KG 抽取的并发当前是**按文档孤立**的，且上传分发会**串行化**：

- `kg_ingest.extract_graph(...)`（`backend/app/services/kg_ingest.py`）对**每次调用各自** `cf.ThreadPoolExecutor(max_workers=workers)`，一窗口一 future。
- `_run_extraction`（`sqlite_repository.py:1130-1139`）以 `workers=self.settings.kg_extract_workers` 调它。
- 所以 N 个文档同时抽取 = N 个独立池 = **N×workers 个线程**，互不协调、**没有全局上限**。`KG_EXTRACT_WORKERS` 现为 1000，多文档并行会瞬间打出几千并发。
- 上传分发：`upload_sources`（`routes.py:165-194`）用 `background_tasks.add_task(process_source, ...)`，而 Starlette **同一请求的 BackgroundTasks 顺序执行** → 一次请求传 N 个文件会**逐个**抽取；窗口池里同一时刻只有一个文档，单文档窗口数 < 全局上限时利用不满。

实测佐证（Analog CMOS 笔记本 5 本教科书，~7.9M 字符）：单本 6–9.5 分钟；`/ask` 正常 ~4s、抽取期飙到 67–490s。

**ask 被饿死的根因已查实**：`OpenAI(...)` 客户端（`core/llm.py:48-56`）**未配置 httpx 连接池上限** → 用 SDK 默认 `max_connections=1000`。抽取打满即占满整个连接池，ask 的那一次调用拿不到连接、排在抽取后面 → 几百秒。

## 目标

1. **两级并发**：
   - **窗口级（全局）**：所有文档的所有窗口（一次 `extract_window` LLM 调用）共享**一个进程级全局预算** `KG_EXTRACT_WORKERS`（env，重启生效）。
   - **作业级（文档）**：最多 `KG_JOB_CONCURRENCY` 个文档同时抽取，把多文档的窗口同时灌进全局窗口池、保持其满载。
2. **两种上传都快**：逐个上传 / 一整批上传（无论"一次请求多文件"还是"多次请求"）都尽量快，且总并发受控。
3. **FIFO**：窗口级按提交顺序共享预算；作业级超过 `KG_JOB_CONCURRENCY` 的文档按 FIFO 等作业槽。
4. **ask 优先**：抽取期交互式 ask 不被饿死。
5. KG 抽取的并发调度收敛为**一个独立模块**（窗口池 + 作业池）。

## 非目标

- 不改窗口大小算法（`plan_window_size` 不动，仍以 `kg_extract_workers` 作并发目标）。
- 不做跨文档公平轮转（用户选 FIFO；作业级并发已让多文档自然重叠）。
- 不做运行时可调并发（env + 重启即可）。
- 不引入独立进程/服务、不引入 asyncio 重写（A 方案，单进程共享线程池）。

## 设计（方案 A：全局共享线程池）

### 1. 新模块：`backend/app/services/kg/scheduler.py`（两个进程级单例池）

- **窗口池** `ThreadPoolExecutor(max_workers=KG_EXTRACT_WORKERS)`：所有文档的所有窗口都进这一个池；FIFO 由 executor 内部队列天然保证。
- **作业池** `ThreadPoolExecutor(max_workers=KG_JOB_CONCURRENCY)`：每个 `process_source`（一个文档的完整管线）作为一个作业进这里；最多 `KG_JOB_CONCURRENCY` 个文档同时抽取。

> **两池必须分离**：作业线程会阻塞等待窗口 future；若作业线程占用窗口池槽会死锁。分成两个独立池后，即使全部作业线程都在等窗口，窗口池线程照常跑窗口——无死锁。

接口（小而清晰）：
- `submit_window(fn, /, *a, **k) -> Future` —— 窗口任务进窗口池。
- `submit_job(fn, /, *a, **k) -> Future` —— 文档抽取作业进作业池（fire-and-forget；`process_source` 自己处理异常与状态，沿用现有逻辑；附一个 done-callback 防御性记录意外异常）。
- `max_workers()` / `job_concurrency()` —— 供日志/测试。
- `reset()` —— 仅测试用重建两池。

懒加载：首次使用时按 settings 建池（settings 已加载），锁保护单例创建；池随进程长驻（空闲线程廉价、I/O 型）。

### 2. 上传分发改为作业池（替代顺序 BackgroundTask）

`upload_sources`（`routes.py`）当前 `scheduler=lambda sid: background_tasks.add_task(repo.process_source, sid)` 改为 `scheduler=lambda sid: kg_scheduler.submit_job(repo.process_source, sid)`。

- 一次请求多文件：N 个 source 各自 `submit_job`，最多 `KG_JOB_CONCURRENCY` 个并发跑（不再被 BackgroundTasks 串行）。
- 多次请求：每个请求各 `submit_job`，同样进同一个作业池、受同一上限约束。
- 响应仍立即返回（`submit_job` 非阻塞）。`background_tasks` 形参可移除。

### 3. `extract_graph` 改为用窗口池

`kg_ingest.extract_graph` 不再自建 `ThreadPoolExecutor`；改为：

```python
from app.services.kg.scheduler import submit_window
...
futs = [submit_window(extract_window, client, els, w.section_path, doc_type, idx)
        for idx, (w, els) in enumerate(pairs)]
for fut in futs:
    try:
        ns, es = fut.result(); nodes += ns; edges += es
    except Exception:
        failed += 1
```

- 每次 `extract_graph` 仍**只收自己这批 future**，故"单源抽取完成"语义不变。
- `workers` 形参不再用于建池（窗口池容量来自 env）；删除该形参，`_run_extraction` 同步去掉传参。
- `extract_graph` 跑在**作业池线程**里，只 submit 窗口 + 等 future，不占窗口池槽 → 无死锁。

### 4. ask 优先（连接池留余 + 端点留余）

ask 的 `chat_json`（答案合成）**不经过 scheduler**（只有抽取窗口经过），从不排在抽取队列后。再保证两层余量让它真正快：

- **连接池余量（核心修复）**：`core/llm.py` 建 `OpenAI(...)` 时传 `http_client=httpx.Client(limits=httpx.Limits(max_connections=KG_EXTRACT_WORKERS + KG_ASK_RESERVE, max_keepalive_connections=KG_ASK_RESERVE))`。抽取最多占 `KG_EXTRACT_WORKERS` 条连接，永远给 ask 留 `KG_ASK_RESERVE`（默认 64）条；突发连接用完即回收，仅保活 `KG_ASK_RESERVE` 条。
- **端点余量**：`KG_EXTRACT_WORKERS`（如 1000）远低于 deepseek-flash 并发限额（2500），端点侧也始终有余量。

> 不需要复杂抢占：抽取被窗口池限在 `KG_EXTRACT_WORKERS`，连接池与端点都留余量，ask 的单次调用自然秒级返回。

### 5. 数据流

`upload_sources → submit_job(process_source)`[作业池, ≤KG_JOB_CONCURRENCY 并发] `→ _run_extraction → extract_graph`（窗口 `submit_window`）[窗口池, ≤KG_EXTRACT_WORKERS] `→ canonicalize → store_kg → 标 extracted`。

- **逐个上传**：每个文档一个作业；前一个没占满窗口池时，后一个进来即用空闲槽。
- **批量上传**：最多 `KG_JOB_CONCURRENCY` 个作业并发，多文档窗口同时灌满窗口池。
- 两种都在 `KG_EXTRACT_WORKERS` 总预算内、受控且尽量满载。

## 配置变更（`core/config.py` / `.env.example`）

- `KG_EXTRACT_WORKERS`：语义从"每文档窗口并发"改为"**全局窗口并发上限**"（值不变，含义更强）。
- 新增 `KG_JOB_CONCURRENCY`（默认 8）：**同时抽取的文档数上限**（作业池容量）。
- 新增 `KG_ASK_RESERVE`（默认 64）：连接池为 ask 预留的连接数。
- `plan_window_size` 签名/逻辑不变。

## 接口与边界

| 单元 | 职责 | 依赖 |
|---|---|---|
| `kg/scheduler.py` | 两个全局单例池：窗口池(`submit_window`/FIFO) + 作业池(`submit_job`) | settings, concurrent.futures |
| `routes.upload_sources` 分发 | 每个 source → `submit_job(process_source)`（替代顺序 BackgroundTask） | kg/scheduler |
| `kg_ingest.extract_graph` | 窗口化 + `submit_window` + 收本源结果 + canonicalize | scheduler, extract_window |
| `core/llm` | LLM 客户端，连接池容量 ≥ 抽取上限 + ask 预留 | httpx, settings |

## 测试（正确性）

- **窗口池**：`reset()` 后用容量=2 建池，提交 N 个带"并发计数 + barrier"的窗口任务，断言**同时在跑 ≤ 2**；`submit_window` 结果正确；FIFO 顺序（记录提交序验证）。
- **作业池**：容量=2，提交 3 个带 barrier 的作业，断言同时跑 ≤ 2。
- **批量重叠**：并发 `submit_job` 两个多窗口 `extract_graph`，窗口级并发计数峰值 **> 单文档窗口数**（证明两文档窗口同时在窗口池）、且 **≤ KG_EXTRACT_WORKERS**（旧实现会到 2×workers）。
- **上传分发**：mock scheduler，断言 `upload_sources` 对每个 source 调 `submit_job`、不再用顺序 BackgroundTask。
- **extract_graph 走窗口池**：注入小容量池，结果与旧实现一致、窗口确经过窗口池（计数）。
- **ask 优先**：单测 `core/llm` 客户端 httpx `max_connections == KG_EXTRACT_WORKERS + KG_ASK_RESERVE`。
- 既有 `tests/kg/*`、`test_adaptive_windows.py` 全绿（extract_graph 结果不变）；`scripts/check.sh` 绿。

## 风险

- **`KG_EXTRACT_WORKERS` 语义变更**：每文档→全局。`.env.example`/README 注明；用户当前设的 1000 正好是想要的全局上限。
- **作业并发放大后台 embedding 并发**：每个作业还会起后台 embed（每作业一路 × `EMBED_CONCURRENCY`）。embed 走**独立端点**(dashscope)、与 chat 不抢；默认 `KG_JOB_CONCURRENCY=8` 可接受，必要时下调。
- **小文档利用率**：若 `KG_JOB_CONCURRENCY × 单文档窗口数 < KG_EXTRACT_WORKERS`，窗口池未满（大量小文档场景）。可调高 `KG_JOB_CONCURRENCY`，代价是更多并发 parse/embed。权衡，env 可调。
- **两池长驻**：进程级单例不显式 shutdown（服务长驻可接受）；测试用 `reset()` 避免串味。
- **httpx**：确认 OpenAI SDK 接受自定义 `http_client`（接受）。
