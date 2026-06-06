# SQLite 写入提速与去锁（方案 C）设计

- 日期：2026-06-06
- 状态：设计已与用户确认，待用户复核 spec
- 范围：仅 `backend/app/services/sqlite_repository.py`（必要时 `backend/app/core/config.py`）
- 决策前提：驱动是**本地单用户**批量抽取时的 `database is locked`；已评估并放弃迁移 PostgreSQL（单用户用不上 PG 的并发写，迁移 ~50–77h 属过度工程，PG 留作未来多用户方向）。

## 背景与诊断

app（单 uvicorn 进程）批量重抽时报 `database is locked`，即使 `busy_timeout=30s`。根因（非 SQLite 扛不住单用户，而是配置+写模式）：
1. `_connect` 未设 `synchronous` → 默认 **FULL**，每次 commit 都 fsync，写慢、写锁持有久。
2. `store_kg` 把一本书 2.6 万行（对象+关系）塞**一个事务**，持锁数十秒。
3. 嵌入写（`_embed_objects_batch` / 元素嵌入）用线程池**每批各自开连接并发写**，与大事务对抢同一个 SQLite 写锁。
4. `KG_EXTRACT_WORKERS` 高 + 作业池并发抽多本 → 多个并发写者裸抢写锁，超过 30s 即失败。

SQLite 写本质单写者，无法真并发写。本方案在 SQLite 内**让写更快 + 让写有序排队（不裸抢）**，在不降并发前提下消除锁失败。

## 目标 / 非目标

**目标**
- 不降并发（保留 `KG_EXTRACT_WORKERS` 高值与作业池并发）下，消除批量抽取的 `database is locked`。
- 提升写入吞吐（commit 更快、写压更低）。

**非目标**
- 不迁移 PostgreSQL。
- 不改抽取/检索/合并的业务逻辑、不改 `NotebookRepository` 接口、不动路由与其他模块。
- 不引入连接池库/ORM。

## 组件

### C1 — PRAGMA 提速（`_connect`）
现状 `_connect`（`sqlite_repository.py:150-156`）仅设 `foreign_keys` / `journal_mode=WAL` / `busy_timeout`。在其后追加：
```python
connection.execute("PRAGMA synchronous = NORMAL")     # WAL 下安全；去掉 commit 的 fsync 等待
connection.execute("PRAGMA cache_size = -65536")       # 64MB 页缓存（负值=KB）
connection.execute("PRAGMA temp_store = MEMORY")       # 临时表/索引走内存
connection.execute("PRAGMA mmap_size = 268435456")     # 256MB 内存映射读
```
所有连接（读写）都生效（这些是 per-connection PRAGMA）。`synchronous=NORMAL` 在断电时可能丢"最后几个未 checkpoint 的事务"，**不损坏库**；本地单用户 + 抽取可重跑，可接受。

### C2 — 单写者串行化（写锁 + `_write()`）
- `SQLiteRepository.__init__` 新增 `self._write_lock = threading.RLock()`（`repository()` 为 `@lru_cache` 单例，进程内唯一；RLock 容忍同线程嵌套写）。
- 新增上下文管理器：
```python
from contextlib import contextmanager

@contextmanager
def _write(self):
    """串行化写事务：进程内同一时刻只有一个写者进 SQLite，
    并发写线程在 Python 层排队而非裸抢 SQLite 写锁。"""
    with self._write_lock:
        with self._connect() as db:
            yield db
```
- **规则**：所有执行 `INSERT/UPDATE/DELETE/REPLACE`（含 `CREATE`/迁移以外的写）的事务块，把 `with self._connect() as db:` 改为 `with self._write() as db:`；**纯读方法保持 `_connect()` 不加锁**（WAL 并发读不受影响）。
- 写方法清单（实现时逐一覆盖，按域归类）：
  - 抽取路径：`store_kg`、知识/元素嵌入写（见 C3）、`_run_extraction`（extraction_runs 增改 + 该源旧 KG 删除）、`_set_source_status`。
  - KG 维护：`write_clusters`、`rebuild_unified_kg`（候选/`unified_kg_state` 写）、`_mark_unified_kg_dirty`、`set_merge_decision`、`delete_notebook_kg`、`delete_source`。
  - CRUD/治理：notebook/source 创建更新删除、`concept_whitelist_add/remove`、object_schema 写等。
  - 例外：`_migrate` / `_seed` 在服务起步、单线程执行，可不改（不并发）。
- **完整性校验（测试项）**：grep 审计——确保没有 `INSERT|UPDATE|DELETE|REPLACE` 残留在非 `_write()` 的 `_connect()` 块内（迁移/seed 除外）。

### C3 — 嵌入解耦（并发算 / 单事务写）
现状 `_embed_objects_batch` 与元素嵌入：线程池里每批**边算向量边各自开连接写**（N 个并发小写抢锁）。改为算/写分离：
- 线程池只**并发计算向量**（`embedder.embed_texts` 的 API 并发不变），每批返回 `[(id, vector), ...]`，**不写库**；
- 计算失败的批次照旧 log + 跳过（保持 best-effort）；
- 收集所有成功向量后，**一次 `_write()` 事务** `executemany("INSERT OR REPLACE ...")` 全量写入。
对 `knowledge_embeddings`（`_embed_objects_batch`）与 `element_embeddings`（`_embed_source`/`_embed_and_store`）都这样做。结果：每源 N 次并发写 → 1 次串行写。

### C4 — `store_kg` 大事务切块
现状 `store_kg` 在一个事务里插完全部对象+关系。改为**分块提交**：
- 先按 `CHUNK=1000` 分块插入对象（每块一个 `_write()` 事务），再按 `CHUNK` 分块插入关系；
- 本地 id→DB id 映射（`local_to_id`）在分块前一次性预分配，保证跨块关系仍能正确 remap；
- 之后再做 C3 的嵌入写、缓存失效与 dirty 标记。
效果：持锁不再一次数秒，排队写者可在块间插入。**权衡**：失去整源原子性（崩溃可能留半本）；`_run_extraction` 逐源自清 + 可重跑，可接受。

## 测试

- **并发去锁测试（关键）**：用 `repo` fixture（临时库，启用本方案的 PRAGMA + 写锁），起 N(≥8) 个线程并发执行"store_kg 式批量写 + 嵌入式批量写"，断言：① 全程无 `sqlite3.OperationalError: database is locked`；② 所有对象/关系/向量计数正确落库。直接复现旧 bug 并验证修复。
- **PRAGMA 测试**：`_connect()` 后 `PRAGMA synchronous` 返回 1（NORMAL）、`temp_store`、`mmap_size`、`cache_size` 为设定值。
- **嵌入解耦测试**：给定 K 个对象，断言向量全部写入且写事务次数显著小于批次数（算/写分离生效）。
- **store_kg 切块测试**：插入 > 1 块的对象+关系，断言全量落库、跨块关系 remap 正确、无丢失。
- **写完整性审计测试**：扫描 `sqlite_repository.py`，断言无 `INSERT/UPDATE/DELETE/REPLACE` 出现在非 `_write()` 写块内（迁移/seed 白名单除外）。
- **回归**：现有 backend 全量测试（221 passed/1 skipped 基线）保持绿；`scripts/check.sh` 通过。

## 风险与权衡

| 项 | 权衡 | 缓解 |
|---|---|---|
| `synchronous=NORMAL` | 断电丢最后事务（不损坏库） | 本地单用户、抽取可重跑 |
| 全局写锁 | 写串行化；仅进程内有效，不跨进程 | app 单进程；维护脚本停后端单独跑 |
| `store_kg` 切块 | 失整源原子性，崩溃留半本 | 逐源自清 + 可重跑 |
| 漏改某写路径 | 漏掉的并发写仍可能锁 | 写完整性审计测试 + 并发去锁测试兜底 |
| `_write()` 包住读写混合方法 | 该方法读期间也持写锁，略降并发 | 仅影响含写的方法；纯读不受影响，可接受 |

## 非范围（明确不做）
- 不做 `KG_JOB_CONCURRENCY` / `KG_EXTRACT_WORKERS` 的默认值调整（保留用户的高并发）。
- 不引入后台单写线程+队列（写锁已足够；若未来仍不够再升级为队列）。
- 不迁 PG、不上 pgvector。
