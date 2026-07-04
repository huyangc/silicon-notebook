# 按核数自动调参(core-aware autotune)设计

日期:2026-07-04
状态:设计已确认,待写实现计划

## 背景与动机

部署环境为单机,当前 16 核,后续升到 64 核。希望「部署时自动判断核数、生成对应参数」,免去每换一台机器手动改旋钮。

但一次并行算法评审(84 个计算热点)得出的核心结论是:**这套后端的墙钟时间绝大部分不吃本地 CPU 核**,原因分三类:

- 🟡 **服务端绑定**:所有 KG 抽取、四条 embedding 路径、rerank、merge-review、概念描述、逐节报告——上限是**远程模型/embed 端点**的并发。本项目所有模型走 URL 端点接入(不在本机跑推理),故这台机器的核数与它们的吞吐**无关**。按本机核数放大这些旋钮只会触发 429 + 指数退避,反而更慢。
- 🔴 **SQLite 单写绑定**:所有 `*_streamed` 写、簇写回、merge 候选 upsert、向量落库——由进程级 `RLock` 串行化,是正确性不变量,加核无用。
- 🟢 **核绑定(原生、释放 GIL)**:hnswlib 建索引/查询、scipy SpMV(PPR)、BLAS 余弦、orjson 多进程解析——**只有这一类** 64 核能真正提速。

因此,「按核数生成参数」这个需求的**正确形态**是:

> 只把 🟢 核绑定旋钮跟着 `os.cpu_count()` / `nproc` 走;🟡 服务端绑定与 🔴 单写绑定的旋钮一律**不**按本机核数推导。

## 目标 / 非目标

**目标**
1. 部署时自动探测核数,推导并应用**核绑定**旋钮的默认值,16→64 核零手工改动。
2. 显式设置的环境变量**永远优先**(自动值只填未设的量)。
3. 启动时打印一行「自动调参报告」,说明选了什么、什么被刻意留默认,消除「为什么升了核没变快」的困惑。
4. 覆盖两个入口:在线服务(经 `scripts/dev.sh` / `scripts/prod.sh`)与离线重建 CLI(`scripts/batch_ingest.py`,不经启动脚本)。

**非目标(刻意不做)**
- **不**按本机核数缩放服务端绑定旋钮:`KG_EXTRACT_WORKERS`、`KG_JOB_CONCURRENCY`、`EMBED_CONCURRENCY`、`KG_ASK_RESERVE`。它们跟随远程端点容量,与本机核数解耦;需要时由运维显式调。
- **不**自动开启多 worker。`scripts/prod.sh` 现为 `--workers 1`,且带有明确理由(进程内缓存 VectorCache/抽取池不跨 worker 共享,多 worker = N× 内存 + N 份不一致缓存,非更高吞吐)。尊重该既有决定;多 worker 只作 README 文档化的**手动 opt-in** + 代价说明。
- **不**动缓存尺寸(`VECTOR_CACHE_MAX_ENTRIES`/`SCALE_IDX_CACHE_MAX`)。那是 RAM 绑定而非核绑定,应作为按内存推导的独立后续特性。

## 设计概览

按「Python 是否必须在 numpy/BLAS 导入**之前**拿到该值」把职责劈成两个组件:

| 值 | 谁读它 | 何时必须就位 | 放哪 |
|---|---|---|---|
| ANN 建索引线程数 | Python(hnswlib `set_num_threads`) | 运行时(晚绑定 OK) | **组件 1:config.py** |
| 回填进程池上限 | Python(CLI 默认值) | 运行时 | **组件 1:config.py** |
| `OMP/OPENBLAS/MKL_NUM_THREADS` | BLAS/OpenMP 运行时,在**其首次导入时**读取 | 解释器启动前 | **组件 2:shell 前导** |

组件 1 必须进 `config.py` 的另一个理由:离线重建 CLI **不经过** dev/prod.sh,只有进程内解析才能同时覆盖服务端与 CLI。

---

## 组件 1 — 进程内 cpu 感知默认值(`app/core/config.py`)

### 1a. 新增 `kg_cluster_ann_threads`

`config.py` 已是 pydantic-settings v2(`BaseSettings` + `model_config`,已导入 `field_validator`/`model_validator`)。

新增字段(遵「pydantic env alias 坑」:可配置项须用 `validation_alias`,**不能**用失效的 `Field(env=...)`):

```python
# 0 = auto:未显式设置时,在 model_validator 里解析成 min(cpu_count, cap)。
# 显式设 KG_CLUSTER_ANN_THREADS=N 则原样采用(N>=1)。
kg_cluster_ann_threads: int = Field(0, validation_alias="KG_CLUSTER_ANN_THREADS")
```

在类内新增(或并入现有的)`model_validator(mode="after")` 解析哨兵:

```python
@model_validator(mode="after")
def _resolve_core_bound_defaults(self):
    if self.kg_cluster_ann_threads <= 0:
        import os
        cores = os.cpu_count() or 1
        object.__setattr__(self, "kg_cluster_ann_threads", min(cores, 32))
    return self
```

封顶 **32**:HNSW 图构建有链路争用 + 该 rep 矩阵(N×1024 f32)受内存带宽限制,超 ~32 线程收益递减;16 核 → 16,64 核 → 32。

> 注意 pydantic-v2 alias 坑的第二面:一旦用了 `validation_alias`,`Settings(kg_cluster_ann_threads=...)` 这种按字段名构造会失效,需按 alias 名构造。测试里若要注入值,用 alias。

### 1b. 把线程数接到 kg_merge

`_ann_candidates` / `_run_shard`([kg_merge.py:178-249](../../../backend/app/services/kg_merge.py))是**纯模块级函数**,不持有 settings。故把线程数作参数逐层传入,默认保持 `1`(对不传的既有调用者零行为变化):

- `_ann_candidates(..., ann_threads: int = 1)` → 传给内层 `_run_shard`;把 `index.set_num_threads(1)`(kg_merge.py:203)改为 `index.set_num_threads(max(1, ann_threads))`。
- `cluster_seeds(..., ann_threads: int = 1)`(kg_merge.py:296,keyword-only 段)→ 调用处 `_ann_candidates(..., ann_threads=ann_threads)`(kg_merge.py:323)。
- 真实重建调用点在 repository 方法内(有 `self.settings`):[sqlite_repository.py:6305 / 6343 / 6475](../../../backend/app/services/sqlite_repository.py),各处补 `ann_threads=self.settings.kg_cluster_ann_threads`。

对照事实:同一个 hnswlib 库在 [scale_index.py:284/296/311](../../../backend/app/services/kg/scale_index.py) 建索引时**不调** `set_num_threads`(默认吃满所有核),唯独 kg_merge 这条被 `set_num_threads(1)` 钉死单核——这是全仓唯一「天花板是人为 pin、非 GIL/非服务端/非 SQLite」的热点,是本设计的头号收益点。

### 1c. 放宽回填进程池默认

[batch_ingest.py:595](../../../backend/app/services/batch_ingest.py) 现为:

```python
_BACKFILL_DEFAULT_WORKERS = min(8, os.cpu_count() or 1)  # 64 核白闲 56 核
```

改为 `min(32, os.cpu_count() or 1)`(已与用户确认取 32 而非 16)。**保持为模块级常量,不新增 env 旋钮**——`--workers` CLI 参数已是覆盖入口,足够。**别到 64**:单写 SQLite 的 `executemany` 阶段 + 每批 IPC pickle 在 ~16-24 处封顶,再加 worker 近乎无收益(真正的剩余收益是把 SELECT/parse/executemany 三段流水线化,属另一后续项,不在本设计范围)。同步更新 CLI `--help` 文案中的默认值提示。

---

## 组件 2 — shell 自动调参前导(`scripts/autotune.sh`)

新增 `scripts/autotune.sh`,由 `dev.sh` 与 `prod.sh` 在 `source .env` **之后**、启动 python **之前** `source` 引入。职责:仅设置那些必须早于解释器的 BLAS/OpenMP 线程变量。

```bash
# scripts/autotune.sh — 仅设「必须在 numpy/BLAS 导入前就位」的核绑定线程变量。
# 原则:显式已设的一律不动;AUTOTUNE=0 整体关闭。被 dev.sh/prod.sh source。
if [[ "${AUTOTUNE:-1}" == "1" ]]; then
  # 跨平台探核数
  if command -v nproc >/dev/null 2>&1; then
    _CORES="$(nproc)"
  else
    _CORES="$(sysctl -n hw.ncpu 2>/dev/null || echo 1)"
  fi
  # 允许测试注入:CORES 覆盖探测值
  _CORES="${CORES:-$_CORES}"

  # BLAS/OpenMP:GEMV/SpMV ~2-4 线程即压满带宽;封顶 8,既够用又给请求并发留核。
  _BLAS="$(( _CORES < 8 ? _CORES : 8 ))"
  for _v in OMP_NUM_THREADS OPENBLAS_NUM_THREADS MKL_NUM_THREADS NUMEXPR_NUM_THREADS; do
    if [[ -z "${!_v:-}" ]]; then export "$_v=$_BLAS"; fi
  done

  echo "autotune: cores=${_CORES} → BLAS(OMP/OPENBLAS/MKL)=${_BLAS};" \
       "ANN_THREADS 由 config 按核推导(见后端首行日志);" \
       "模型端旋钮不变(KG_EXTRACT_WORKERS/KG_JOB_CONCURRENCY/EMBED_CONCURRENCY)。" >&2
fi
```

- **仅当未设**才 export(`-z "${!_v:-}"`),显式优先。
- `AUTOTUNE=0` 逃生阀。
- 不碰 `--workers 1`。
- `dev.sh` / `prod.sh` 各加一行 `source "$ROOT_DIR/scripts/autotune.sh"`(在 .env 加载之后)。

### 离线 CLI 的 BLAS 变量

`scripts/batch_ingest.py` 直接 `python` 起、不经启动脚本。其 KG 重建的主导成本是 hnswlib(由**组件 1** 的 `kg_cluster_ann_threads` 治理,进程内生效,CLI 自动吃到);scipy/BLAS 在重建路径占比小。故:

- CLI **默认**已从组件 1 得到 ANN 线程数,无需额外动作。
- 若要给 CLI 也调 BLAS,文档说明可 `source scripts/autotune.sh && python scripts/batch_ingest.py ...`。不做隐式改动,避免惊喜。

---

## 数据流(启动时序)

```
npm run start
  └─ scripts/prod.sh
       ├─ source .env                      # 既有:显式配置就位
       ├─ source scripts/autotune.sh       # 新:未设则 export OMP/OPENBLAS/MKL=min(cores,8);打印报告
       └─ python -m uvicorn app.main:app --workers 1
            └─ Settings()  (config.py)
                 └─ model_validator: kg_cluster_ann_threads=0 → min(cores,32)   # 组件 1
                 └─ 首行日志追加:resolved kg_cluster_ann_threads / backfill 默认
```

## 可观测性

- shell 前导打印一行(见上)。
- 后端首行启动日志(现已打印 db/storage/log 绝对路径)追加:`kg_cluster_ann_threads=<n>`、回填默认 workers=`<n>`,以及一句「模型端并发旋钮为固定默认,与核数无关」。让运维一眼看清自动值。

## 测试

1. **config 解析**(pytest):
   - `KG_CLUSTER_ANN_THREADS` 未设 + mock `os.cpu_count()` 返回 16 / 64 → 解析为 16 / 32(封顶)。
   - 显式设 `KG_CLUSTER_ANN_THREADS=4` → 原样 4(显式优先),不被覆盖。
   - 用 alias 名构造/注入(遵 pydantic-v2 alias 坑)。
2. **kg_merge 接线**(pytest):`_run_shard` 用传入的 `ann_threads` 调 `set_num_threads`,而非硬编码 1;`cluster_seeds` 默认仍为 1(未传时零行为变化)。
3. **回填默认**:`_BACKFILL_DEFAULT_WORKERS` == `min(32, cpu_count)`;`--workers` 显式覆盖仍生效。
4. **shell**:`shellcheck scripts/autotune.sh`;用 `CORES=64` 注入 dry-run,断言仅 export 未设变量、`AUTOTUNE=0` 时零 export、打印那行报告。

## 交付物 / 影响文件

- `backend/app/core/config.py`:新增 `kg_cluster_ann_threads` + `model_validator` 解析。
- `backend/app/services/kg_merge.py`:`_ann_candidates`/`_run_shard`/`cluster_seeds` 加 `ann_threads` 参数;解掉 `set_num_threads(1)`。
- `backend/app/services/sqlite_repository.py`:三处 `cluster_seeds(...)` 传 `ann_threads=self.settings.kg_cluster_ann_threads`。
- `backend/app/services/batch_ingest.py`:回填默认 `min(8→32, cpu)`;`--help` 文案。
- `scripts/autotune.sh`:新增。
- `scripts/dev.sh` / `scripts/prod.sh`:各 `source` 一行。
- 后端启动日志一行追加。
- 测试:config / kg_merge 接线 / 回填默认 / shell dry-run。
- `.env.example`:补 `KG_CLUSTER_ANN_THREADS`(注明 0=auto)与 `AUTOTUNE` 说明。
- `README.md` / `README_zh.md`:新增「按核数自动调参」小节(通用口径:自动调什么、刻意不调什么与原因、多 worker 手动 opt-in 及代价、`AUTOTUNE=0` 关闭);遵「提交文档保持通用」,不写本机绝对路径。

## 风险 / 已知坑

- **pydantic-v2 alias 坑**:`validation_alias` 后按字段名构造 Settings 失效,测试注入须用 alias 名。
- **服务端旋钮误伤**:严格不把 `KG_EXTRACT_WORKERS` 等纳入自动缩放——否则 64 核触发 429。文档明确写清。
- **多 worker 内存**:若有人手动开多 worker,提醒每 worker N× RAM(一个 49 万节点索引 ≈ 2GB)+ 后台 KG job 落点不定。README 里作为带警告的手动项。
- **CLI 的 BLAS**:离线 CLI 不自动设 OMP,仅靠组件 1 的 ANN 线程数(主导项),这是刻意取舍;需要时文档指引手动 `source autotune.sh`。
