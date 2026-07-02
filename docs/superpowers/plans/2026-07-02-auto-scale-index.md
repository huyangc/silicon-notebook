# 大库自动建检索索引 Plan

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development。

**背景**:检索索引现在只有手动入口(前端徽章/CLI),错过就让所有 O(N) 暴力回退成为稳态(真机 49万库正是如此)。需求方拍板:**「大」复用分享/拷贝的定义**(`notebook_copy_stats` 的 `copyable` 取反,sqlite_repository.py:1379-1392:字节>50MB 或 chunks+nodes>5000)→ 大库自动建/重建索引。

**Goal**:大库无需人工干预获得/保持检索索引;默认走 idle 低峰窗口(避免高峰抢 16 核);小库行为不变;所有既有手动入口/徽章/去重机制复用不动。

**Tech Stack**:纯后端;pytest。解释器 `/opt/homebrew/Caskroom/miniconda/base/bin/python`,worktree `backend/` 跑测试。基线 `pytest tests/ -q` = **1446 passed, 1 skipped**(本分支基于 master 5a85c2d)。

## Global Constraints
- 复用不重造:判定走 `notebook_copy_stats()["copyable"]`;入队走既有 `trigger_scale_index_rebuild(nb, when=..., mode="auto")`(自带 `_scale_building`/`_scale_idle_queue` 去重与 409 语义——auto 路径不抛 409,静默跳过);状态机/徽章(queued/building)零前端改动。
- 读路径兜底绝不阻塞请求:只做 O(1) 集合判断 + 后台入队,失败吞掉(fail-open)。
- 配置 pydantic-settings v2 `validation_alias`:`scale_index_auto_enabled: bool = Field(True, validation_alias="SCALE_INDEX_AUTO_ENABLED")`、`scale_index_auto_when: str = Field("idle", validation_alias="SCALE_INDEX_AUTO_WHEN")`(取值 idle|now)。
- 判定成本控制:`notebook_copy_stats` 是 5 个 COUNT(索引可覆盖),写路径每来源一次可接受;读路径兜底必须先过**进程内 once-set**(`_auto_index_checked: set[nb]`)再算 COUNT,避免每查询 5 COUNT。

## Task 1: `maybe_auto_index` + 两类触发点 + 配置

**Files:** `backend/app/services/sqlite_repository.py`、`backend/app/core/config.py`;Test 新建 `backend/tests/test_auto_scale_index.py`。

- [ ] Step 1 写测试(先红):
  - `test_large_nb_upload_triggers_idle_enqueue`:monkeypatch `notebook_copy_max_rows=0`(一切库皆「大」)+ spy `trigger_scale_index_rebuild`;走一次抽取完成路径(直接调 `maybe_auto_index(nb)` 单元级 + 集成点见下)→ 被调一次,`when=="idle"`。
  - `test_small_nb_no_trigger`:默认阈值小库 → 不触发。
  - `test_indexed_fresh_no_trigger`:大库但索引存在且不 stale → 不触发(判定用 `scale_index_status` 的 state;或更便宜的内部等价判断,实现者选,报告说明)。
  - `test_auto_disabled_no_trigger`:`scale_index_auto_enabled=False` → 不触发。
  - `test_retrieval_fallback_triggers_once`:大库无索引,连续两次走 KG 对象检索无 ANN 回退 → `maybe_auto_index` 只实际评估/入队一次(once-set 生效);且检索返回值不受影响。
  - `test_when_now_spawns_build`:`scale_index_auto_when="now"` → `trigger_scale_index_rebuild` 收到 `when="now"`。
- [ ] Step 2 实现:
  - config 两项(见约束)。
  - `maybe_auto_index(notebook_id)`:enabled 关→return;`notebook_id in self._auto_index_checked`→return(读路径进来才查集合;写路径每次都可评估但也先查集合防重复入队——入队成功或判定「不需要」都加入集合;**索引后续再变 stale 时集合会挡住重触发——写路径在 `_mark_unified_kg_dirty` 级别 discard 该 nb** 使下轮上传重新评估,实现在 `_mark_unified_kg_dirty` 里加一行 `self._auto_index_checked.discard(nb)`);然后:copyable→加集合 return;state 非 suggested/stale(exists 且新鲜/building/queued)→加集合 return;否则 `trigger_scale_index_rebuild(nb, when=settings.scale_index_auto_when, mode="auto")` try/except 吞异常,加集合。
  - 写路径触发点:`_run_extraction` 成功收尾处(现有 `incremental_fuse_source` 调用之后)调 `maybe_auto_index`;`rebuild_unified_kg` 成功收尾也调(rebuild 后索引必 stale,大库该重建)。
  - 读路径兜底:`_kg_object_candidates`(或 `_retrieve_scored` 的无 ANN 回退入口,取一处最稳的)进回退分支时调 `maybe_auto_index`(其内部 once-set 保证 O(1))。
  - `trigger_scale_index_rebuild` 若现实现对不 eligible/进行中抛 HTTPException/409:auto 调用方捕获吞掉(或加 `raise_=False` 参数,实现者选更干净者)。
- [ ] Step 3 回归:`pytest tests/test_auto_scale_index.py -q` + `-k "scale or share or copy"` + 全量(基线 1446)。
- [ ] Step 4 提交 `feat(kg): 大库自动建检索索引(复用分享「大」定义,默认 idle 低峰;写路径+检索回退双触发)`。

## Task 2: 文档
- [ ] README.md + README_zh.md 部署段落补一句:大库自动建索引(默认 idle 窗口),`SCALE_INDEX_AUTO_ENABLED`/`SCALE_INDEX_AUTO_WHEN` 可调;通用口径,不写机器特定内容。随 Task 1 同 PR。

## 收尾
- rebase origin/master → push → `gh pr create --base master`。
