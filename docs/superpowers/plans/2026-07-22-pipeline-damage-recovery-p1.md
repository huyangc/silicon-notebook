# 流水线损坏善后 P1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐设计文档 A4/A5/A6 三项——① `batch_ingest ingest` 子命令的 hash 跳过「认账过早」，parse 中途中断的源会永久变成空源；② element 向量**完全没有**缺失查询与补齐路径（chunk 侧与 KG 节点侧都有）；③ README 双语把只写不读的 `.jsonl` 描述成「续跑依据」。

**Architecture:** 纯后端 + 文档。Task 1/2 都改 `backend/app/services/batch_ingest.py`（故合并为一个实现单元，不并行）；Task 2 另加 `maintenance.py` 的两个查询、`source_embedding.py` 的 element 批量嵌入、`ports.py` 登记。**不改 schema、不改产物格式、不加配置项、不动前端。**

**Tech Stack:** Python 3.13 / SQLite / pytest。

**Worktree:** `/Users/hzf/workspace/silicon_notebook/.claude/worktrees/kg-extraction-embedding-review-829762`，分支 `claude/pipeline-damage-recovery-p1`（从合入 P0 后的 master 切出）。后端命令工作目录 `<worktree>/backend`。

**Spec:** `docs/superpowers/specs/2026-07-22-pipeline-damage-recovery-design.md`（已批准）的 A4 / A5 / A6。P0（A1/A2/A3）已由 #323 合入。

## Global Constraints

- 不改 schema、不新增配置项、不动前端、不新增 API 端点。
- **补齐路径产出的向量必须与正常路径逐字节同构**：同样的截断规则、同样的空文本过滤。否则补出来的向量与主路径不一致，检索质量会静默劣化——这比不补更糟。
- 幂等：任何补齐命令重跑第二遍应为 0 项可补。
- **禁止**在 worktree 里跑 `npm install`（`frontend/node_modules` 是软链，会写穿主 checkout）。
- **禁止**在本 worktree 使用 `git stash`（共享 checkout，会撞上其它分支的既有 stash——P0 期间真发生过）。
- 每个 Task 完成跑指定测试子集；全部完成后跑后端全量。

---

### Task 1: `ingest` 子命令的跳过判据改为「有没有 elements」（A4）

**Files:**
- Modify: `backend/app/services/batch_ingest.py`（`run_ingest`）
- Test: `backend/tests/test_batch_ingest.py`

**背景（已核实的事实，不要重新推演）：**
- `run_ingest._one` 现在只有二元判定：`already_ingested()` 命中即 `skipped`（`batch_ingest.py:386` 一带）。
- 而 `file_hash` 是在 **INSERT 时**写的，早于 parse（`source_ingestion.py:378` 写 `file_hash=digest`、`:374` 写 `parse_status="queued"`）。所以一个 parse 中途被 Ctrl-C 的源，下次跑 `ingest` 会被判为已摄取而**永久变成空源**。
- `run_all` 已经用三分流修好了同类问题（`batch_ingest.py:624-638`），判据是 `sources_with_elements`。**本 Task 是把同一判据搬到 `ingest`，不是发明新逻辑。**
- ⚠ 但 `ingest` 不是 `run_all` 的三分流：`ingest` 是**无 KG 阶段**，没有「已有 KG → 跳过」这一档。正确形状是**两分流**。

- [ ] **Step 1: 改判据**

在提交线程池之前取一次 `parsed = repo.maintenance.sources_with_elements(notebook_id)`，`_one` 改为：

```
sid = source_id_by_hash(repo, notebook_id, sha256_bytes(content))
sid is None          → upload_sources(...)        → "uploaded"
sid in parsed        → 真的已摄取                  → "skipped"
否则(有行但无 elements) → repo.process_source(sid)  → "reparsed"
```

`counts` 增加 `"reparsed": 0` 一档。

- [ ] **Step 2: 处理同一次运行内的重复文件**

`parsed` 是进池前取的快照。若输入目录里有两个内容相同的文件：第一个走 upload（它会同步 parse 出 elements），第二个 hash 命中但**不在快照里** → 会被误判成「需要重新 parse」，白跑一遍。

修法：在本次运行内维护一个「已由本次上传/重解析处理过的 sid」集合（线程安全，`_one` 在池里并发跑），命中即 `skipped`。**不要**改成每次重查数据库——那是每文件一次全表 DISTINCT 扫描。

- [ ] **Step 3: 不要顺手「修」LLM 暴露**

已核实：`upload_sources(scheduler=None)` 本来就**同步**调用 `process_source`（`source_ingestion.py:383-386`），而 `should_extract_kg` 在 `kg_auto_extract` 关闭时仍会因「该 notebook 已有 KG」返回 True（`source_ingestion.py:411-413`）。

也就是说 `run_ingest` 今天就可能在「无 LLM」的名义下抽 KG——**这是既有行为，本 Task 新增的 reparse 分支调的是同一个 `process_source`，不引入任何新暴露**。不要在本 Task 里改这个（会扩大爆炸半径，且需要单独判断是不是真该改）。如果认为值得修，记下来单独提。

- [ ] **Step 4: 测试**

- 既有 `test_run_ingest_dedup_skips_on_rerun`（`test_batch_ingest.py:169` 一带）必须继续绿：**有 elements** 的源重跑仍是 `skipped`。
- 新增：造一个 hash 已存在、但**没有 `source_elements`** 的源 → 重跑 `ingest` 判为 `reparsed` 而非 `skipped`，且跑完真的有了 elements。这条直接钉住 A4 要修的 bug。
- 新增：同一次运行传入两个内容相同的文件 → 一个 `uploaded`、一个 `skipped`，**不出现** `reparsed`（Step 2 的回归防线）。

---

### Task 2: element 向量的缺失查询与补齐（A5）

**Files:**
- Modify: `backend/app/repositories/sqlite/maintenance.py`（两个新查询）
- Modify: `backend/app/services/source_embedding.py`（element 批量嵌入）
- Modify: `backend/app/services/batch_ingest.py`（`backfill_element_embeddings` + 接进 `run_embed`）
- Modify: `backend/app/repositories/ports.py`（登记新 maintenance 方法）
- Test: `backend/tests/test_batch_ingest.py`（或就近的 embedding 测试文件）

**背景（已核实）：**
- chunk 侧有完整一套：`missing_chunk_embedding_rows`（`maintenance.py:410`）、`count_missing_chunk_vectors`（`:487`）、`backfill_chunk_embeddings(missing_only=True)`（`batch_ingest.py:411`）、`run_embed`（`:820`）。KG 节点侧也有。**element 侧全仓零命中**——写了一半只能整源重跑。
- `element_embeddings` 主键是 `element_id`；`replace_element_vectors` 是 **`INSERT OR REPLACE` 纯 upsert**（`embedding_store.py:40-46`），**不是先删后插**。所以「只补缺失」是安全的，不必整源重嵌。
- 正常路径 `embed_source`（`source_embedding.py:122-160`）的构造是：`pending = [el for el in elements if el.text.strip()]`，文本取 `el.text[:self.settings.embed_truncate_chars]`。

- [ ] **Step 1: 两个查询（照 chunk 侧同构）**

`missing_element_embedding_rows(notebook_id)` 与 `count_missing_element_vectors(notebook_id)`：
`source_elements` JOIN `sources` 限定 notebook + `NOT EXISTS (SELECT 1 FROM element_embeddings v WHERE v.element_id=e.id)`。

⚠ **必须排除空文本元素**（`TRIM(e.text) != ''` 或等效）。`embed_source` 会跳过它们，所以它们永远不会有向量——不排除的话，补齐命令会永远报「还有 N 个缺失」、每次跑都试图嵌入空串，成为一个永不收敛的脏状态。这条是本 Task 最容易漏的。

- [ ] **Step 2: element 批量嵌入**

在 `source_embedding.py` 加一个按 `element_id` 批量嵌入并落库的方法。三条硬要求：

1. **截断必须用 `self.settings.embed_truncate_chars`**，与 `embed_source` 一致。**不要**复用 `embed_chunks_batch`——它硬编码 `text[:2000]`（`source_embedding.py:308`），默认值恰好也是 2000 所以现在看不出差别，但一旦调大 `EMBED_TRUNCATE_CHARS`，补出来的向量就和主路径不同构了。
2. `replace_element_vectors` 的签名是 per-source（`source_id, notebook_id, rows`），所以待补行**必须按 `source_id` 分组**后逐组落库。
3. 沿用既有的 per-batch 失败隔离风格（失败批记 warning 后跳过，不炸整轮）。

- [ ] **Step 3: CLI 接线**

`backfill_element_embeddings(repo, notebook_id, conc)` 照 `backfill_chunk_embeddings` 的形状写（含 `embedder_configured` 未配置即返回 0、进度打印），接进 `run_embed`（`batch_ingest.py:820-844`）：盘点 → 补齐 → 复盘，与 chunk/节点两侧并列。

- [ ] **Step 4: ports 登记 + 文档**

`ports.py` 按 chunk 侧两个方法（`:754`、`:762` 一带）的写法登记新方法。若架构守卫要求同步 `ownership_manifest.py` 或契约夹具，按 `--rebaseline-surface` / `--rebaseline-callers` 重生成，并**核对 diff 只含本次新增的站点**。

README 双语的 `embed` 子命令说明补上「element 向量」（原文只说 chunk/节点）——与 Task 3 一并改，避免两次改同一段。

- [ ] **Step 5: 测试**

- 造「有 elements、element_embeddings 缺一部分」的库 → 补齐后全部有向量，且**第二遍跑为 0 项可补**（幂等）。
- **空文本元素不被计入缺失**（Step 1 的回归防线，最容易漏）。
- 补出的向量与 `embed_source` 走同一条构造：断言截断用的是 `embed_truncate_chars` 而非硬编码 2000——把该配置调成非 2000 的值来验，否则这条测试在默认值下无检出力。

---

### Task 3: README 双语订正（A6）

**Files:**
- Modify: `README.md`（约 :1180）
- Modify: `README_zh.md`（约 :996）

- [ ] **Step 1: 改掉「续跑依据」的错误说法**

两处都把 `<storage>/batch_ingest/<notebook>.jsonl` 描述成重跑的依据：

- `README.md:1180` — "progress is written to `<storage>/batch_ingest/<notebook>.jsonl` and a re-run resumes automatically"
- `README_zh.md:996` — "进度写 `<storage>/batch_ingest/<notebook>.jsonl`,中断后重跑自动续"

事实：该文件**只写不读**（全仓无任何读取方），续跑完全靠对数据库状态的查询推导。改为如实说明——文件是运行日志，续跑靠 DB 判据；并点明各子命令的判据不同（`ingest` 看 hash + elements、`kg` 看最近一次 extraction_run 是否 completed、`embed` 看向量是否存在）。

保持 README 的通用产品口径，不写机器特定路径。

---

## 验证

- [ ] 后端全量：`cd backend && python -m pytest -q`。⚠ 用 `${PIPESTATUS[0]}` 取退出码，别被管道末端骗（P0 期间踩过）。
- [ ] ⚠ **提 PR 前必须 rebase 到最新 master 再跑一次全量**——P0 就是在 rebase 后才暴露出第 9 个依赖点（新 master 带进来的测试）。只跑自己分支原理上看不见。
- [ ] 架构守卫全绿；契约夹具若重生成，核对 diff 只含本次新增站点。

## 提交

三个 Task 各自成 commit，rebase 到 master 保持线性，push 后 `gh pr create --base master`。PR 描述链接设计文档，写明本期覆盖 A4/A5/A6，P2（体检 endpoint）/P3（看板改造）不在其中。
