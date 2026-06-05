# 自适应抽取窗口（按文档大小 + 并发选窗口、等长切分）设计

日期：2026-06-05
分支：`claude/modest-hodgkin-8cb2c0`
关联：`2026-06-05-flow-procedure-extraction-design.md`（二期）、记忆 `kg-extract-llm-timeout`

## 背景与诊断（实测）

innovusUG_sample（129k 字符 / 800 元素）在固定 `KG_WINDOW_TARGET_CHARS=9000` 下抽取：

- 24 次 LLM 调用：15 ok / 7 retry / 2 失败；单次输出 **p50 9617 / max 15688 tok**；延迟 **p50 91s / p90 184s / max 204s**；吞吐 **~120 tok/s**。
- 抽取 wall-clock 207s——被**最慢的一块（~204s）**拖着，因为 15 窗口 ≈ 16 并发 = 一个批次。
- 超时根因（记忆已记）：**单窗口输出太大**，固定 9000 字符在密集手册上生成 8 分钟级 token 量。

## 关键模型（决定方案）

令 `C`=内容字符数，`T`≈120 tok/s（实测，16 并发稳定），`d`≈1.36 输出tok/字符（innovus），`workers`=并发。

- **大文档（窗口数 N ≥ workers）**：`wall ≈ d·C/(T·workers)`，**与窗口大小无关**（W 在分子分母约掉）。→ 窗口越大越省（调用少、重复 prompt 少），上限只受超时/长尾约束。
- **小文档（N ≤ workers，单批次）**：`wall = 最慢的那一块 ≈ d·W/T`。→ 窗口越小且**越等长**越快（填满并发、长尾最短）。

固定 9000 在两个区间都不优：大文档不够省（其实可更大）、小文档长尾大且易超时。

## 目标

每篇文档抽取 **≤ 5 分钟**，同时消除超时/丢窗。已知 deepseek-flash 并发上限 1500 → 取 **workers=100**；超时已置 240。

## 可行性（workers=100）

`C_max ≈ 300·T·workers/(2d) ≈ 1.3M 内容字符 / 5min`——覆盖所有真实单文档（整本 innovusUG 量级内）。sample（129k）≈ 90s。

## 方案

### 选窗口大小（按文档 + 并发，量化三档的连续形式）

```
level = clamp(C / workers, w_min=4000, w_max=8000)
N     = ceil(C / level)
n     = ceil(C / N)          # 等长切分，杜绝 8000+1000 长尾
```
- `C/workers`：让窗口数 ≈ 并发数（小文档自动切小、填满并发；大文档落到上限）。
- `w_max=8000`：单块最坏 ~180s，离 240s 超时留 ~1.3× 余量；再大危险。
- `w_min=4000`：再小只是徒增调用/prompt 开销，对 5min SLA 无意义（小文档本就快）。
- **等长 `n=ceil(C/N)`**：例 9000 字 → level 4000 → N=3 → n=3000 → [3000,3000,3000]（而非贪心 [4000,4000,1000]）。这是"切均匀消长尾"的精确形式。

行为示例（workers=100）：

| C（内容字符） | level | N | n（等长） |
|---|---|---|---|
| ≤4000 | — | 1 | C |
| 9000 | 4000 | 3 | 3000 |
| 129k（sample） | 4000 | 33 | ~3910 |
| 600k | 6000 | 100 | 6000 |
| 1.0M | 8000 | 125 | 8000 |

### 并发

`KG_EXTRACT_WORKERS` 由 16 → **100**（在根 `.env` 设置；代码默认仍保守 16，避免影响其它部署）。`_run_extraction` 已把 `workers=settings.kg_extract_workers` 传入 `extract_graph`，无需改线路。

## 改动点（均已核对真实代码）

1. `backend/app/core/config.py`：新增 `kg_window_min_chars=4000`、`kg_window_max_chars=8000`；`kg_window_target_chars` 改为「可选硬覆盖」（默认 0 = 自适应；>0 = 固定，向后兼容/手动调试）。`kg_extract_workers` 默认保持 16（部署侧 .env 设 100）。
2. `backend/app/services/kg_ingest.py`：新增纯函数 `plan_window_size(content_chars, workers, w_min, w_max, override=0) -> int`，返回等长窗口字符数 `n`。
3. `backend/app/services/sqlite_repository.py:1109-1115` `_run_extraction`：`raw_text` 已在手，调用 `plan_window_size(len(raw_text), settings.kg_extract_workers, settings.kg_window_min_chars, settings.kg_window_max_chars, override=settings.kg_window_target_chars)` 得 `n`，传给 `extract_graph(..., n=n, ...)`。
4. 根 `.env`：`KG_EXTRACT_WORKERS=100`（可选显式写出 `KG_WINDOW_MIN_CHARS/MAX_CHARS`）。

`make_windows` 不改——它已按传入 `n` 贪心打包；我们传等长的 `n=C/N`，输出即近似等长（末块余量 < n，无大尾巴）。

## 默认值（已与用户确认）

- 三档 `{4000,6000,8000}` 的连续形式 `clamp(C/workers,4000,8000)`；等长切分。
- workers=100（deepseek-flash 限 1500，安全）。
- 大文档优先省钱用大窗口，小文档优先并发用小窗口。

## 测试（正确性，非效果）

- `plan_window_size` 单测：
  - C≤w_min → 1 个窗口（n=C）。
  - C=9000, workers=100 → n=3000（N=3）。
  - C=129206, workers=100 → N=33、n≈3916（每块 ∈[w_min,w_max]）。
  - C=1_000_000, workers=100 → level 封顶 8000、n=8000。
  - `override>0` → 直接返回 override（向后兼容）。
  - 不变式：`w_min ≤ n ≤ w_max`（除单窗口/override）；`N×n ≥ C`（覆盖全文）。
- 既有 `tests/kg/*`（windowing/extract/canonicalize）随之全绿。
- 不在一期/二期/本期跑全量效果回归（沿用"全部完成后统一看效果"）。

## 风险 / 待验证

- **100 并发下单调用吞吐是否仍 ~120 tok/s**：16 并发已验，100 未验。落地后先跑一次 `KG_EXTRACT_WORKERS=100` 抽取，确认单调用 latency 不显著退化；若退化，线性加速打折（但仍优于现状）。
- `w_max=8000` 是长尾/超时余量的折中；要更激进（10000）需把超时提到 300。
- OpenAI SDK 默认 `max_connections=1000`，100 并发在池内，无需改连接池。
- 成本：窗口变小→调用增多→输入 token 上升，但输出 token 不变（大头），且 deepseek prompt caching 让重复的 system/指令近乎免费 → 增量很小。
