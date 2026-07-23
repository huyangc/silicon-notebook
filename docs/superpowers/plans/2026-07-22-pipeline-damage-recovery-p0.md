# 流水线损坏善后 P0（止血）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修掉审计中两个 P0 缺口——① scale 索引全量重建就地覆盖活目录且加载端零校验（唯一会「静默地错」的一格）；② 启动清算不覆盖 `queued`/`parsing` 导致源永久搁浅，同时把清算收拢到服务端启动路径（顺带修掉「离线 CLI 启动会把 pending 行刷成 failed」的已知老坑）。

**Architecture:** 纯后端。Task 1/2 只动 scale 索引的存取两端（`scale_artifact_store.py` 的 `save_full` 与 `kg/scale_index.py` 的 `load_scale_index`），**不改产物格式、不改 manifest 字段、不改构建器**。Task 3 把恢复调用从仓储构造搬到 lifespan 启动路径，并给清算加一条 SQL；**不新增 parse_status 取值**（复用已有 `failed`，前端无改动）。三个 Task 互不依赖，可并行实现、分别提交。

**Tech Stack:** Python 3.13 / FastAPI / SQLite / numpy + scipy + hnswlib / pytest。

**Worktree:** `/Users/hzf/workspace/silicon_notebook/.claude/worktrees/kg-extraction-embedding-review-829762`，分支 `claude/kg-extraction-embedding-review-829762`。所有命令在 worktree 内执行；后端命令的工作目录是 `<worktree>/backend`。

**Spec:** `docs/superpowers/specs/2026-07-22-pipeline-damage-recovery-design.md`（已获用户批准）。本计划覆盖其中的 A1 / A2 / A3。

## Global Constraints

- **不改产物磁盘格式**：文件名、manifest 字段、`ScaleIndex` dataclass 字段一律不动。已部署的索引必须照常加载。
- **older-index-stays-valid 不变量**：老索引的 manifest 可能缺少 `n_nodes` / `has_chunk_ann` 等键。任何新校验**只在键存在时**生效，缺键一律放行，绝不能把老索引判成损坏。
- **宁可退化成「无索引」，也不返回错配的索引**：损坏一律 `return None`（等价于「没建过」，会触发全量重建），绝不 raise 到检索热路径。
- 不在本期引入任何新的 API 端点、新表、新配置项、新 UI。
- 中文注释与既有风格一致；面向用户的文案不暴露技术名词。
- **禁止**在 worktree 里跑 `npm install`（`frontend/node_modules` 是指向主 checkout 的软链，会写穿真树）。
- 每个 Task 完成后跑该 Task 指定的测试子集；三个 Task 全部完成后再跑一次后端全量（注意已知 flake，见「验证」一节）。

---

### Task 1: scale 索引全量重建改原子写（A1）

**Files:**
- Modify: `backend/app/repositories/filesystem/scale_artifact_store.py`（`save_full`、`swap_fold_directory`、模块与方法 docstring）
- Test: `backend/tests/test_scale_artifact_compatibility.py`（新增故障注入用例）

**背景（必读，决定改法）：**
`save_full` 当前直接把活目录交给 `save_scale_index`（`scale_artifact_store.py:79-81`），而后者 `os.makedirs(out_dir, exist_ok=True)` 后逐个就地覆盖 `graph.npz` / `node_ids.npy` / `ann.bin` / …，**manifest.json 最后写**（`backend/app/services/kg/scale_index.py:263-334`）。若此前已有索引，重建中途崩溃 = 旧 manifest 幸存 + 数组已被部分覆盖。
**fold 路径已经做对了**：`prepare_fold_directory` 起 `.tmp`、`swap_fold_directory` 走 `live → .old`、`tmp → live`、`rm .old`。本 Task 是让 full 收敛到这套既有实现，**不是发明新机制**。

**Interfaces:**
- `save_full(notebook_id, artifacts)` 签名与返回值（`dict` manifest）**不变**——`scale_index_builder.py` 的调用点不动。
- `swap_fold_directory(notebook_id, temporary)` 签名不变，但语义扩展为「活目录可以不存在」（首次全量构建时没有 live）。

- [ ] **Step 1: 先读既有约束**

读 `backend/tests/test_scale_artifact_compatibility.py` 与 `backend/tests/test_scale_builder_failure_boundaries.py`，确认是否有测试逐字断言 `swap_fold_directory` 的「三步序列」或 `save_full` 直写活目录。**若有，先在此记录，再决定是修改断言还是保留旧序列另起方法**——不要盲改后让测试红了才发现。

- [ ] **Step 2: 让 `swap_fold_directory` 容忍活目录缺失**

当前 `os.rename(out_dir, old_dir)`（`:103`）在 `out_dir` 不存在时抛 `FileNotFoundError`。fold 从不遇到（fold 只在已有索引时跑），但 full 首次构建必然遇到。

改为：活目录存在才 `live → .old`；不存在则跳过该步、`old_dir` 视为无，后续的回滚与 `rm .old` 相应跳过。**回滚语义保持不变**（只有确实做过 `live → .old` 才需要回滚）。更新 docstring 说明它现在同时服务 full 与 fold。

- [ ] **Step 3: `save_full` 改为 staging + publish**

```
tmp = self.prepare_fold_directory(notebook_id)      # 复用既有 staging（含清理上次残留 .tmp）
manifest = scale_index_module.save_scale_index(str(tmp), **artifacts)
self.swap_fold_directory(notebook_id, tmp)
return manifest
```

注意：
- `prepare_fold_directory` 的方法名此后略显偏窄，**只更新 docstring，不要重命名**——重命名会波及 `ownership_manifest.py` 与既有测试，换不来功能收益。
- `save_scale_index` **一行都不用改**（它只写调用方给的目录）。
- 若 `save_scale_index` 抛异常，`.tmp` 会留在盘上、活目录完好；下次 `prepare_fold_directory` 会 rmtree 掉它。这正是期望行为。

- [ ] **Step 4: 故障注入验证（不做等于没改）**

在 `test_scale_artifact_compatibility.py` 新增两个用例：
1. **已有索引 + 保存中途失败**：monkeypatch `scale_index_module.save_scale_index` 让它写几个文件后抛异常，断言活目录里的 manifest 与数组**仍是上一版**（内容逐字比对），且 `.tmp` 的存在不影响下次加载。
2. **首次构建（无活目录）**：断言 `save_full` 正常完成、活目录被建立、返回的 manifest 正确——即 Step 2 的容忍分支真的走到了。

- [ ] **Step 5: 记录磁盘代价**

staging 会让构建期磁盘峰值约翻倍（大库 ANN 可达 GB 级）。在 `save_full` 的 docstring 里写明这一点（fold 早已是同样的代价，此处只是把它扩展到 full）。**不加配置开关**——原子性不做成可关的。

---

### Task 2: scale 索引加载端完整性校验（A2）

**Files:**
- Modify: `backend/app/services/kg/scale_index.py`（`load_scale_index`）
- Test: `backend/tests/test_scale_artifact_compatibility.py`

**背景：** `load_scale_index`（`:46-108`）当前只判断 `manifest.json` 是否存在，随后无条件 `sp.load_npz` / `np.load` 所有数组，**没有任何 checksum，也没有 manifest 计数与数组长度的交叉校验**。Task 1 之后新的损坏窗口已极小，但**已部署的库里可能已经躺着一份坏索引**，且 `.old` 回滚失败等路径仍会留下不一致目录——所以加载端必须自己能判。

**Interfaces:**
- `load_scale_index(out_dir) -> ScaleIndex | None` 签名不变。返回 `None` 的含义从「没有索引」扩展为「没有索引**或**索引不可信」，两者对调用方等价（都会走重建）。
- 新增模块级 `logging.getLogger(...)`，损坏时 `warning` 一条，包含 `out_dir` 与**具体失配项**（便于运维定位）。不 raise。

- [ ] **Step 1: 确定哪些不变量是真的**

以下两条由 `save_scale_index` / `scale_index_builder.py:264-279` 的写入直接保证，可以直接断言：
- `manifest["n_nodes"] == len(node_ids)`
- `manifest["n_ann"] == len(ann_labels)`
- `manifest["has_chunk_ann"]` 为真时 `manifest["n_chunk_ann"] == len(chunk_ann_labels)`
- `manifest["has_relation_ann"]` 为真时 `manifest["n_relation_ann"] == len(relation_ann_labels)`
- `manifest["has_viz"]` 为真时 `manifest["n_viz_nodes"] == len(viz_ids)`

以下**必须先去构建器核实再决定是否断言**，不要凭直觉写：
- `transition.shape[0]` / `shape[1]` 与 `len(node_ids)` 的关系
- `len(idf)` 与 `len(node_ids)` 的关系
- `chunk_index` 的长度语义与 `manifest["n_chunks"]` 的关系

核实方法：读 `backend/app/services/scale_index_builder.py` 里这几个数组的构造处。**核实不了的就不要加校验**——加一条错的校验会把好索引判成坏的，比不加更糟。

- [ ] **Step 2: 实现校验**

- 所有数组加载包进 try/except（`OSError` / `ValueError` / `EOFError` 等），任一文件缺失或反序列化失败 → 记 warning + `return None`。
- 校验只在 manifest 里**该键存在时**生效（older-index-stays-valid）。
- 任一校验失配 → 记 warning（写明哪一项、期望多少、实际多少）+ `return None`。
- 可选产物（viz / chunk_ann / relation_ann）失配时：**与主产物一视同仁判损坏**。理由是它们的计数与主索引出自同一次写入，失配即证明那次写入不完整。

- [ ] **Step 3: 测试**

新增用例覆盖：
1. 手工构造「manifest 说 n_nodes=100、node_ids.npy 只有 50 行」的目录 → `load_scale_index` 返回 `None`，且日志含失配详情。
2. 删掉 `graph.npz` 但保留 manifest → 返回 `None`（不抛）。
3. **老索引兼容**：构造一份 manifest 里没有 `n_ann` / `has_viz` 等键的目录 → 正常加载，**不得**被判损坏。这条是回归防线，必须有。
4. 正常索引 → 照常加载（已有用例覆盖则复用）。

---

### Task 3: 启动清算收拢到服务端 + 覆盖搁浅源（A3）

**Files:**
- Modify: `backend/app/repositories/sqlite/migrations.py`（`initialize()`、`_recover_interrupted_jobs()`）
- Modify: `backend/app/services/startup_warmup.py`（`run_startup()`）
- Test: `backend/tests/test_knowhow_schema.py`、`backend/tests/test_backfill_knowhow_md.py`、新增用例

**背景与已定决策：**
`SQLiteRepository.__init__` 无条件调用 `_migrator.initialize()`（`backend/app/services/sqlite_repository.py:665`），而 `initialize()` = migrate + **recover** + seed（`migrations.py:1680-1684`）。服务端的 `repository()` 是 `@lru_cache`（`backend/app/api/deps.py:15`），一进程一次；但**离线 CLI 走的是直接 `SQLiteRepository(...)` 构造**（约 20 处），于是每次跑脚本都会执行一遍清算——这就是「离线 CLI 启动会把 pending 行刷成 failed」这个已知坑的根因。

**用户已拍板：恢复只在服务端跑。** 实现方式**不是**给 20 个构造点传参，而是把恢复调用从 `initialize()` 里摘出来，交给 lifespan 启动路径显式调用一次——facade 上 `recover_interrupted_jobs()` 已经存在（`sqlite_repository.py:1032`），无需新增对外成员。

**Interfaces:**
- `SqliteMigrator.initialize()` 语义收窄为 **migrate + seed**（不再含 recover）。`migrate()` / `seed()` / `recover_interrupted_jobs()` 三个公开方法本身不变。
- `startup_warmup.run_startup()` 在拿到 repo 之后、预热之前，显式调用一次恢复。
- **不新增 `parse_status` 取值**：搁浅源落到已有的 `failed`（`frontend/app/vocabulary.ts:15` 已有「解析失败」标签），前端零改动。

- [ ] **Step 1: 把恢复移出 `initialize()`**

`initialize()` 改为只 `migrate()` + `seed()`。**保留** `recover_interrupted_jobs()` 这个公开方法（它就是新的调用入口）。在 `initialize()` 的 docstring 里写明：恢复已移交服务端启动路径，理由是离线 CLI 不该接管这个库。

- [ ] **Step 2: 在 lifespan 启动路径显式恢复**

`startup_warmup.run_startup()` 中，`repo = repository()`（`startup_warmup.py:36` 一带）之后、`warm_open_path_caches` 之前，调用 `repo.recover_interrupted_jobs()`。

必须落在 `readiness.mark_ready()` **之前**——这是既有的关键性质：清算未完成前业务路由一律 503，用户不可能在残骸还没翻正时发起新任务。**不要**把它挪到 `mark_ready()` 之后（那里是遗留 knowhow 重投影的位置，语义不同）。

- [ ] **Step 3: 清算增加搁浅源一条**

在 `_recover_interrupted_jobs()` 里追加：

```sql
UPDATE sources SET status='failed', parse_status='failed',
       error_message='<面向用户的中文文案>', updated_at=?
 WHERE parse_status IN ('queued','parsing')
```

要点：
- `status` 与 `parse_status` **必须同时写**（`SourceStore.set_status` 的既有语义就是两列同值，见 `backend/app/repositories/sqlite/source_store.py:379-380`）。
- 文案参照既有风格（对照同函数里 kg_build_jobs 那条「服务重启导致本次分析中断；已完成内容已保留，可继续分析未完成内容。」），说明「上传后未能开始/完成解析，可重新解析」，**不出现** `queued` / `parsing` / `parse_status` 等技术名词。
- 在函数 docstring 里补一句这条的立论，与既有六条并列。
- **不要**把搁浅源置为 `parsed`：它们没有 `source_elements`，标成「已解析」是谎报，且会让它们从任何「待处理」视图里消失。（补充事实：即使误置为 `parsed` 也不会抽出空 KG——`_kg_target_state` 要求 `sid in sources_with_elements`——但谎报状态本身就不可接受。）

- [ ] **Step 4: 修既有测试**

`test_knowhow_schema.py:172` 一带有「Reopen on the same DB — construction re-runs `_recover_interrupted_jobs`」的用例，`test_backfill_knowhow_md.py` 亦有引用。这些断言的正是本 Task 有意改变的行为：**改成显式调用 `recover_interrupted_jobs()` 来驱动**，而不是依赖构造副作用。`test_ask_jobs.py:95` 已经是显式调用，无需改。

逐个检查，不要用「跑一遍看哪个红了」代替通读。

- [ ] **Step 5: 变异验证（这条最容易假绿）**

1. **CLI 不再误伤**：造一行 `parse_status='queued'` 的源 → 直接 `SQLiteRepository(settings)` 构造（模拟 CLI）→ 断言该行**仍是** `queued`。再断言 `knowhow_rows` 的 `pending` 行同样未被刷成 `failed`（这是顺带修掉的老坑，必须有断言钉住）。
2. **服务端仍然恢复**：同样的库，调用 `repo.recover_interrupted_jobs()` → 断言该行变 `failed` 且 `error_message` 非空。
3. **恢复仍在 ready 之前**：断言 `run_startup()` 里恢复的调用位置早于 `mark_ready()`。⚠ 只做「删除」变异不够——把恢复调用**移到** `mark_ready()` 之后，确认测试仍能抓到；若抓不到，说明断言写松了（源码断言的 `[\s\S]*?` 容易越过块边界，先 slice 到具体函数体再断言）。

---

## 验证

三个 Task 全部完成后：

- [ ] 跑后端全量：`cd backend && python -m pytest -x -q`。⚠ 已知偶发失败先归因再认领，别默认是自己引入的（`kg_merge` 的 2s 性能硬阈值较脆；带真 `.env` 全量跑会被 `llm_cache.db` 污染）。
- [ ] 架构守卫：确认没有触发 facade surface / callers 契约漂移。若 Task 1 动了 `ScaleArtifactStore` 的方法集合，检查 `backend/app/repositories/ownership_manifest.py` 是否需要同步。
- [ ] 手动确认 Task 1 的产物在真实索引目录上仍可加载（用现有 base 库跑一次 `mode=full` 重建，再打开知识图谱视图）。

## 提交

三个 Task 各自独立成 commit，最后 rebase 到 master 保持线性，push 后 `gh pr create --base master`。PR 描述里链接设计文档，并写明本期只覆盖 A1/A2/A3（P1 的 `ingest` 三分流、element 向量补齐查询、README 订正不在本 PR）。
