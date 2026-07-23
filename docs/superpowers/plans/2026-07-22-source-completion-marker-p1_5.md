# P1.5 源完成标记 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `sources` 补一列 `chunked_at`（持久化完成标记）+ 一个进程内活跃租约（内存 dict），把「这个 `extracted`+0chunk 的源当初是合法 0 产物还是分块失败」「此刻是否正被本进程处理」从不可判定变可判定。这是 P2 体检 endpoint 的前置依赖，本身不含任何体检/UI 逻辑。

**Architecture:** 纯后端。schema 变更收敛到单列（追加 `_migration_25` + bump `SCHEMA_VERSION` 两处字面量）。写入点在 `process_source`（取/放内存租约、写 elements 时归零 `chunked_at`）与 `build_chunks_for_source`（分块成功置 `chunked_at`；失败留 NULL——这是全设计枢纽）。启动清算**不新增**对 sources 的改动（内存租约方案的红利）。**不改** `parse_status` 语义、不建索引、不动前端、不改四份文档（golden 是这里的 schema 文档）。

**Tech Stack:** Python 3.13 / SQLite / pytest。

**Worktree:** `/Users/hzf/workspace/silicon_notebook/.claude/worktrees/kg-extraction-embedding-review-829762`，分支 `claude/pipeline-damage-recovery-p2`（已含 P0+P1）。后端命令工作目录 `<worktree>/backend`，解释器 `/opt/homebrew/Caskroom/miniconda/base/bin/python`。

**Spec:** `docs/superpowers/specs/2026-07-22-source-completion-marker-design.md`（已批准）。

## Global Constraints

- schema 红线：**追加新 `_migration_25` 并 bump `SCHEMA_VERSION`**，不塞进已封版的旧迁移。`SCHEMA_VERSION` 有**两处独立字面量**（`migrations.py:15` 与 `sqlite_repository.py:252`），必须一起改，漏一处静默漂移。
- 不改 `parse_status` 语义；不建 `chunked_at` 索引（留 P2）；不动前端。
- golden `schema_contract.txt` 用 `UPDATE_SCHEMA_GOLDEN=1` 重生成，**不手写**。
- 存量回填是硬要求（E 节）：不回填会让合法纯标题/短文 md 被 H3 集体误报——上线一墙假警报。
- **禁止**在 worktree 跑 `npm install`（软链会写穿主 checkout）；**禁止**用 `git stash`（共享 checkout，会撞别分支既有 stash）。
- 每个 Task 完成跑指定测试；全部完成后 rebase 到最新 master 再跑全量（P0/P1 的教训：只跑自己分支看不见并行 PR 引入的新依赖）。

---

### Task 1: schema 迁移 + 版本 bump + 回填

**Files:**
- Modify: `backend/app/repositories/sqlite/migrations.py`（`SCHEMA_VERSION:15`、追加 `_migration_25`）
- Modify: `backend/app/services/sqlite_repository.py`（`SCHEMA_VERSION:252`）
- Test: `backend/tests/test_legacy_db_compat.py`（新增部署库升级用例 + golden 重生成）

- [ ] **Step 1: bump 两处 SCHEMA_VERSION**

`migrations.py:15` 与 `sqlite_repository.py:252` 都从 24 改 25。**不要**碰
`diagnostics_runtime.py:32` 的 `SCHEMA_VERSION = 1`（那是诊断事件 schema、与 DB 无关）。

- [ ] **Step 2: 追加 `_migration_25`**

在 `_migration_24`（`migrations.py:1540`）之后追加，内容见 spec「Schema 变更」节：
`add_column_if_missing(db, "sources", "chunked_at", "TEXT")` + 回填 UPDATE（`WHERE
chunked_at IS NULL AND parse_status IN ('parsed','extracting','extracted')` 置
`updated_at`）。docstring 写明回填立论（否则纯标题 md 被 H3 集体误报）。

确认 `add_column_if_missing`（`migrations.py:31`）是可重入的 PRAGMA 守卫——它就是为这个
用的。

- [ ] **Step 3: golden 重生成 + 部署库升级用例**

- 用 `UPDATE_SCHEMA_GOLDEN=1 /opt/homebrew/.../python -m pytest tests/test_legacy_db_compat.py -k contract` 重生成 `schema_contract.txt`；diff 应只多 `sources.chunked_at` 一行，列序在 sources 段末尾（`memory_id` 之后）。**核对 diff 只有这一行**，多出别的说明 golden 漂了别的东西。
- 新增 `test_deployed_v24_db_upgrades_adds_chunked_at`：照 `test_legacy_db_compat.py` 里 `_migration_24` 的同款写法（在 v24 库上开仓储、断言 `chunked_at` 列出现）。**这条证明版本闸对已部署库不短路**——schema 红线的核心。

- [ ] **Step 4: 迁移变异验证**

构造一个 v24 部署库（有 parsed/extracted/uploaded/failed/metadata-only 各态的源），跑
`_migration_25`，断言：parsed/extracting/extracted 的源 `chunked_at = 各自 updated_at`；
uploaded/queued/parsing/failed/metadata-only 的源 `chunked_at IS NULL`。这条直接钉住
回填规则不误伤。

---

### Task 2: 版本硬编码测试批量修正

**Files（bump 后会红，逐个通读改，不靠「跑一遍看哪个红」）:**
- `backend/tests/test_sqlite_migrator_component.py`、`test_multi_domain_bases.py`、`test_memory_kg_schema.py`、`test_repository_v9_fixture.py`、`test_source_asset_migration.py`、`test_legacy_db_compat.py`（含 `test_v24_...` 函数名）

- [ ] **Step 1: 先跑一次全量定位所有版本断言**

`pytest -q 2>&1 | grep -i "24\|SCHEMA_VERSION"` 只是起点。**逐个文件通读**被报红的断言，区分：
- 纯版本号断言（`== 24` → `== 25`）：直接改。
- 函数名/夹具名带 `v24`/`_migration_24` 的：这些是**历史锚点**，语义是「测 v24 库能升级」——**不要**改成 v25（那会丢掉对 v24 的覆盖）。新增 v25 的用例（Task 1 Step 3 已做部署库升级），旧的 v24 锚点保留。
- 判断哪些是「断言当前版本」（改）vs「断言历史版本可升级」（保留）——这个区分错了会悄悄删掉回归覆盖。

- [ ] **Step 2: 改完确认**

`pytest tests/test_sqlite_migrator_component.py tests/test_multi_domain_bases.py tests/test_memory_kg_schema.py tests/test_repository_v9_fixture.py tests/test_source_asset_migration.py tests/test_legacy_db_compat.py -q` 全绿。

---

### Task 3: `chunked_at` 写入点（store 方法 + process_source + 分块）

**Files:**
- Modify: `backend/app/repositories/sqlite/source_store.py`（新增 `mark_chunked`）
- Modify: `backend/app/services/source_chunking.py`（分块成功置标记）
- Modify: `backend/app/services/source_ingestion.py`（写 elements 时归零）
- Test: `backend/tests/test_source_*.py`（就近的 source 测试文件）

- [ ] **Step 1: `source_store.mark_chunked(source_id, ts)`**

一条 `UPDATE sources SET chunked_at=? WHERE id=?`。参照 `set_status`（`source_store.py:371`）
的写法但**不**碰 status/parse_status（正交）。`insert_source`（`:308`）不改——新行走列默认
NULL。

- [ ] **Step 2: 分块成功置标记（枢纽，放对位置）**

在 `build_chunks_for_source`（`source_chunking.py:43`）**正常返回前**调
`self.sources.mark_chunked(source_id, self.now())`。**必须放在这里而非 process_source**：
这样 `chunk_and_embed_source`（`:67`）、`scripts/build_chunks.py` 等所有分块路径统一打标。

⚠ 含 0 chunk 的成功也要置标记（纯标题 md）——`build_chunks` 返回空列表时
`replace_source_chunks` 照常提交（0 行），mark_chunked 照常置值。确认 mark_chunked 在
「返回 0 chunk」这条路径上也会执行（别被 early-return 跳过）。

- [ ] **Step 3: 写 elements 时归零 `chunked_at`**

在 `source_ingestion.py:551-572` 的 `with write() as db:` 事务内（clear_source_extraction_state
+ replace_elements 那块），加 `UPDATE sources SET chunked_at=NULL WHERE id=?`。**同一事务**——
新代 elements 落库即令旧分块完成失效，无崩溃窗口。可折进 `clear_source_extraction_state`
或就地一条。

- [ ] **Step 4: 分块失败什么都不写（确认，不是改）**

`source_ingestion.py:586-593` 的 except 分支**保持不写 `chunked_at`**（留 NULL）。这是 H3
的损坏信号。现有的 log + `knowledge_counts_cache.invalidate` 保留。**这步是确认没有意外
在失败路径写标记，不是新增代码。**

- [ ] **Step 5: H3 可判定性测试（全设计要证的核心，不做等于没证明）**

构造两个 `parse_status/elements/chunks` 完全相同的源：
1. 「分块成功产 0 chunk」（跑真实 build_chunks 于纯标题输入 → `chunked_at` 有值、0 chunk）
2. 「分块失败」（monkeypatch build_chunks 抛异常 → `chunked_at` NULL、0 chunk）

断言：用判据 `elements>0 AND chunks=0 AND chunked_at IS NULL` 时，只有 #2 命中、#1 不命中。
**变异验证**：把 Step 2 的 mark_chunked 删掉 → #1 也变 NULL → 该测试红（证明标记真的在
区分两支）。

---

### Task 4: 活跃租约（内存）+ 启动清算不变量

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（承载租约 dict + 锁，镜像 `_kg_building`）或 `knowledge_lifecycle.py`（若 runtime 所有权在那）
- Modify: `backend/app/services/source_ingestion.py`（进入取、finally 放）
- Test: `backend/tests/test_startup_recovery_ownership.py`（扩不变量）+ 新增租约用例

- [ ] **Step 1: 租约结构**

镜像 `knowledge_lifecycle.kg_building` / `kg_building_lock`（`sqlite_repository.py:585-586`
别名同一对象的先例）：一个 `dict[source_id → started_at]` + `threading.Lock`。放在 runtime
的合适所有者上，让 process_source 能拿到。**照 `_kg_building` 的所有权链走**，别自己新起一套。

- [ ] **Step 2: 进入取、finally 放**

- 进入 `process_source`（`source_ingestion.py:439`）函数体最顶、`:469` 置 parsing 之前：
  锁下 `active[source_id] = now()`。
- 给 `:470` 的 try **加 finally**（早于 `:687` 的 `maybe_enqueue_scale_fold`）：锁下
  `active.pop(source_id, None)`。确认 finally 覆盖所有出口（成功 return、except、
  KgBuildAborted 等）。

- [ ] **Step 3: 启动清算不变量（不新增清算，加测试钉不变量）**

`_recover_interrupted_jobs` 对 sources **不新增改动**——spec 已论证内存租约不参与清算。
但要在 `test_startup_recovery_ownership.py` 扩一条：断言 `mark_ready()` 触发那一刻内存
租约为空（沿用它已有的「mark_ready 探针抓快照」手法，见 P0 那条
`test_run_startup_settles_stranded_sources_before_marking_ready`）。这钉住「单进程+就绪门
⇒ 清算时刻无活跃源」的不变量。

- [ ] **Step 4: 租约功能测试**

- 进入 process_source 期间 active 集含该 source_id、退出后不含（用一个卡在分块的 monkeypatch
  制造「处理中」窗口，或直接单元测 stamp/pop 逻辑）。
- 异常出口也释放：monkeypatch 某阶段抛异常，断言 finally 仍 pop 掉了租约。

---

## 验证

- [ ] 每个 Task 跑指定测试子集；全部完成后 rebase 到最新 master（`git fetch origin master && git rebase origin/master`）**再跑一次后端全量** `pytest -q`，用 `${PIPESTATUS[0]}` 或重定向取退出码（别被管道末端骗）。
- [ ] 已知 flake 先归因别认领（`test_repository_v9_fixture` 顺序敏感、`kg_merge` 2s 硬阈值脆——见 backend-suite-known-flakes memory）。
- [ ] 架构守卫全绿；若新 store 方法/runtime 成员触发契约漂移，按 `--rebaseline-surface`/`--rebaseline-callers` 重生成并**核对 diff 只含本次新增站点**（P1 踩过三次 test-only patch 污染——给 facade 打桩会进冻结契约，打 maintenance/runtime 内部不会）。

## 提交

四个 Task 各自成 commit（Task 1/2 也可合一个「schema+版本测试」commit），rebase 到 master 保持线性，push 后 `gh pr create --base master`。PR 描述链接 spec 与主设计文档，写明这是 P2 的前置依赖（P1.5），本 PR 只补可判定性、不含体检 endpoint。
