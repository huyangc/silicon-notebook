# 大文档摄取管线:抽取优先 + 后台并发向量化 — 实施计划

> **For agentic workers:** 用 superpowers:subagent-driven-development 逐任务实施。步骤用 `- [ ]` 复选框。

**Goal:** 让大文档(如 innovus 16990 元素)上传后,KG 抽取**立即开始并优先完成**(完成即置 `extracted`/绿),"全元素向量化"不再串行堵在抽取前面,而是**并发(默认 50)+ 逐 batch 落库 + 后台与抽取同时进行**;前端只在真正 `extracted` 时显示绿。

**根因(systematic-debugging 已坐实):**
- `process_source` 顺序 parse→**embed(全部元素串行)**→extract;innovus embed 串行 1700 次 dashscope 调用 ≈ **21 分钟**,KG 抽取被堵在后面没开始(只有 1 条 summary chat)。
- `_embed_source` 是"全部算完再一次性入库",期间无进度、崩了全丢。
- 前端 `.status-parsed` 与 `.status-extracted` 共用绿色 `#177a55`,解析完(0.6s)就变绿,误导"已完成"。
- `_connect()` 仅设 `foreign_keys=ON`,**无 WAL/busy_timeout** → 并发写会 `database is locked`。

**约束/决策:** dashscope 单请求 batch≤10;并发度可配,**默认 50**(账号上限~1800,但 50 已把 21min→~1.5min,稳妥);抽取与元素向量化并发,以 `extracted`(抽取完成)为"绿"的判据;向量化 best-effort、失败隔离。

**通用约定:** 测试 `PYTHONPATH=backend python -m pytest <路径> -v`(fallback python `/opt/homebrew/Caskroom/miniconda/base/bin/python`);commit 末尾加 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`;每任务跑相关 + 受影响既有测试,绿了再 commit。

---

## T1 — config 旋钮
**Files:** `backend/app/core/config.py`
- 加 `embed_concurrency: int = Field(50, env="EMBED_CONCURRENCY")`(并行发出的 batch 请求数)
- 加 `db_busy_timeout_ms: int = Field(30000, env="DB_BUSY_TIMEOUT_MS")`
- 验证 `Settings()` 加载,默认 50 / 30000。

## T2 — DB 并发地基:WAL + busy_timeout
**Files:** `backend/app/services/sqlite_repository.py` `_connect()`
- 在 `PRAGMA foreign_keys = ON` 之后加:`PRAGMA journal_mode = WAL`、`PRAGMA busy_timeout = <db_busy_timeout_ms>`。
- 测试:`_connect()` 后 `journal_mode` 为 `wal`;两个线程各自 `_connect()` 并发 INSERT 同表都成功(无 "database is locked")。

## T3 — 元素向量化:并发 + 逐 batch 落库 + 失败隔离
**Files:** `backend/app/services/sqlite_repository.py` `_embed_source`(重写);可加 `backend/app/services/embedding.py` 助手
- 把 pending 元素按 `embed_batch_size`(10)切 batch;用 `ThreadPoolExecutor(max_workers=embed_concurrency)` 并发提交,每个任务:`embedder.embed_texts(batch)` 成功 → **用本线程独立 `self._connect()` 立刻 INSERT 这 batch 的向量**;异常 → 记录+跳过(不影响其它 batch)。
- 不再"全部算完再入库";每 batch 独立连接(SQLite check_same_thread)。
- 测试:① mock embedder 记录并发(同时在飞的请求数 >1);② 第 k 个 batch 抛错,其余 batch 向量仍落库;③ 全成功时 element_embeddings 行数 == 元素数。

## T4 — 管线:抽取前台优先 + 元素向量化后台并发
**Files:** `backend/app/services/sqlite_repository.py` `process_source`
- 解析+存元素后:启动**后台线程**跑 `self._embed_source(source_id)`(与抽取并发);前台跑 `self._run_extraction(source_id)`;抽取一返回**立刻置 `extracted`(绿)**,不等向量化;随后 `join` 后台线程(让其跑完、记录 `embed done`)再结束 pipeline。
- embed 阶段事件改为后台线程内记录 start/done;extract 不再等 embed。
- 注意:后台 embed 线程内所有 DB 访问走各自 `_connect()`(T2 的 WAL+busy_timeout 保证并发写安全)。
- 测试:用 fake embedder(故意慢/可控)+ fake llm,断言**抽取完成后 status 立即变 `extracted`,无需等向量化结束**;且最终 element_embeddings 落库完成。

## T5 — 前端:绿色只留给 extracted
**Files:** `frontend/app/globals.css`
- 把 `.status-parsed` 从 `.status-extracted` 绿色组移到 `.status-queued,.status-parsing,.status-extracting` 处理中(橙)组。绿色 `#177a55` 仅 `.status-extracted`。
- 验证 `tsc --noEmit`(无类型变更)+ 人工确认中间态不再显示绿。

---

## 验收(真机)
重新上传 innovus → KG 抽取**几分钟内完成、状态变绿、可 KG 问答**;元素向量化在后台 ~1.5min(50 并发)跑完、逐 batch 落库;过程中前端中间态显示"处理中橙"而非绿。

## Self-Review(对照根因)
- 21min 串行堵抽取 → T3 并发(50)+ T4 抽取前台不等 embed ✓
- 全量入库无进度/易全丢 → T3 逐 batch 落库 ✓
- 并发写 locked → T2 WAL+busy_timeout ✓
- 前端误绿 → T5 ✓
- 无占位;类型/签名一致(embed_concurrency/db_busy_timeout_ms 贯穿 T1→T2/T3/T4)。
