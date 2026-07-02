# KG 视图慢路径修复:viz 后台构建 + 检索索引徽章解耦 Plan

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development。

**背景(真机根因,49万对象库)**:打开知识图谱页卡死数分钟。根因三连:
1. `_viz_index`(sqlite_repository.py)没有新鲜 viz 索引时 **`build_viz_index` 同步在 GET 请求里跑**(全图派生+折叠,分钟级,持 GIL 拖慢全进程);
2. 兜底 `_unified_graph_full` 更是全量拉 49万行 payload + Python fold;
3. `_scale_index_version`(#147 后)每次调用仍算 concept_clusters 的 COUNT+MAX(created_at),无 `(notebook_id, created_at)` 索引 → 逐行回表,你这规模秒级×每请求多次。

另:检索索引徽章(page.tsx:3031)渲染门控 `tier === 'base'`,与「已解耦」的状态请求(1005 行注释)不一致 → 非 base 大库看不到入口,建不了索引,死循环。

**Goal**:GET 请求内永不做分钟级工作;大库图谱页显示「构建中」并轮询;任何 eligible 库都能看到/点击检索索引徽章。

**Tech Stack**:FastAPI + SQLite;Next.js page.tsx。解释器 `/opt/homebrew/Caskroom/miniconda/base/bin/python`;测试在 worktree `backend/` 下跑 pytest。

## Global Constraints
- 小库行为不变:对象数 ≤ 阈值(新配置 `viz_sync_build_max_objects`,默认 20000,validation_alias `VIZ_SYNC_BUILD_MAX_OBJECTS`——pydantic-settings v2 必须用 validation_alias,Field(env=) 无效)时保留现有同步懒构建语义(现有测试全绿)。
- stale viz 索引对**展示**是 benign 的:有 stale 就先返回 stale,后台刷新;绝不为了新鲜度让 GET 卡住。
- 后台构建去重:仿 `_scale_building` 的 set+lock 范式(`_viz_building`);build_viz_index 只读 DB、无模型调用,daemon 线程安全。
- API 加性变更:unified_graph 响应新增可选 `viz_building`;unified_kg_status 新增 `viz_building`。不破坏既有字段。
- ⚠ PR#136 教训:给 concept_clusters 加索引可能扰动无 ORDER BY 查询的行序 → Task 2 必须先审计所有 `FROM concept_clusters` 的读点,顺序敏感处锁 ORDER BY(参照 `_stream_seed_reps` 两处 ORDER BY rowid 的先例)。

---

## Task 1: `_viz_index` 大库不同步构建 → 后台 + building 状态(backend)

**Files:** `backend/app/services/sqlite_repository.py`(`_viz_index`、`unified_graph`、`unified_kg_status` 附近)、`backend/app/core/config.py`;Test `backend/tests/test_kg_viz_index.py`(找既有 viz 测试文件,没有就近建)。

- [ ] Step 1 写测试(先红):
  - `test_unified_graph_large_nb_returns_building_placeholder`:monkeypatch `settings.viz_sync_build_max_objects=0`(强制走大库路径),建 nb+少量 KG;调 `repo.unified_graph(nb, level='object', limit=10)` → 返回 `viz_building=True`、nodes==[]、**调用前后断言没有同步产出 viz 索引文件**;且 `nb in repo._viz_building` 或后台线程启动(可 join 等待)。
  - `test_unified_graph_building_then_ready`:等后台建完(轮询 `_viz_building` 清空,cap 数秒)后再调 unified_graph → 正常 bounded 图(nodes 非空,无 viz_building)。
  - `test_unified_graph_small_nb_unchanged`:阈值默认(大)时,小库首调 unified_graph 同步可用(现行为)。
  - `test_viz_stale_served_while_rebuilding`:先建好 viz 索引,改 KG(版本漂移),再调 unified_graph → 立即返回图(stale 数据可用),且后台刷新启动。
  - `test_unified_kg_status_reports_viz_building`。
- [ ] Step 2 实现:
  - config.py:`viz_sync_build_max_objects: int = Field(20000, validation_alias="VIZ_SYNC_BUILD_MAX_OBJECTS")`。
  - repo `__init__` 处(仿 `_scale_building`):`self._viz_building: set = set()`、`self._viz_building_lock = threading.Lock()`。
  - 抽 `_spawn_viz_build(nb)`:guard 进 set → daemon 线程跑 `build_viz_index(nb)`,finally discard;异常仅记日志(仿 `_run_scale_op`)。
  - `_viz_index` 重写决策:
    1. 有效 scale 索引 → 返回(不变);
    2. 进程缓存/磁盘 viz 且版本匹配 → 返回(不变);
    3. 磁盘有 **stale** viz → `_spawn_viz_build(nb)` + 返回 stale idx(新);
    4. 什么都没有:`COUNT(knowledge_objects WHERE nb, status!='deprecated')` ≤ 阈值 → 同步 `build_viz_index`(现行为);> 阈值 → `_spawn_viz_build(nb)` + 返回 None(新)。
  - `unified_graph`:fast-path 拿到 idx(含 stale)照常 bounded;idx 为 None 且 `nb in _viz_building` → 返回 `{"nodes":[],"edges":[],"total_nodes":0,"total_edges":0,"truncated":False,"viz_building":True}`;None 且不在构建(空图/小库)→ 现有 `_unified_graph_full` 兜底不变。
  - `unified_kg_status` 返回 dict 加 `"viz_building": nb in self._viz_building`。
  - `kg_neighbors`:idx None 时已有 `_kg_neighbors_db` 有界兜底,不动。
- [ ] Step 3 跑新测试 + 回归:`pytest tests/ -x -q -k "viz or unified"` 及全量相邻(test_kg_*.py)。
- [ ] Step 4 提交 `perf(kg-viz): 大库 viz 索引改后台构建,GET 返回 building 状态(不再同步全图折叠)`。

## Task 2: concept_clusters 聚合索引 + 行序审计(backend)

**Files:** `backend/app/services/sqlite_repository.py`(schema 索引区 L713 附近 + 审计读点);Test:全量回归即验证。

- [ ] Step 1 审计:grep 所有 `FROM concept_clusters`,列出无 ORDER BY 且消费方顺序敏感的查询(重点 `_stream_seed_reps`/rebuild/cluster_map/`_cluster_input_version`)。顺序敏感处补 `ORDER BY rowid`(先例:PR#136 在 `_stream_seed_reps` 两处)。审计结论写进提交信息。
- [ ] Step 2 加索引:`CREATE INDEX IF NOT EXISTS idx_clusters_nb_created ON concept_clusters(notebook_id, created_at);` → `MAX(created_at)` 变索引 seek、COUNT 走窄索引。
- [ ] Step 3 回归:`pytest tests/ -q`(全量,重点 test_kg_rebuild*/cluster 相关必须绿——聚类顺序不变量)。
- [ ] Step 4 提交 `perf(kg): concept_clusters (nb,created_at) 索引,版本探针聚合免回表`。

## Task 3: 前端 — 徽章解耦可点 + 图谱页 building 轮询(frontend)

**Files:** `frontend/app/page.tsx`。

- [ ] Step 1 徽章(≈L3031):门控 `currentNotebook?.tier === "base" && scaleIndexStatus` → `scaleIndexStatus && (scaleIndexStatus.eligible || scaleIndexStatus.exists)`;并把这段 `<p class=tool-hint>` 改成与 KG 视图徽章(L4058)同款**可点** tag:clickable = eligible && !building && state!=='queued',onClick=confirmBuildScaleIndex(已存在,L1049,本就 tier 无关),不 eligible 时 title 提示「库较小,暂不需要」。两处徽章文案/六态 label 保持一致(对齐、省略号截断,UI 精致要求)。
- [ ] Step 2 图谱页 building:UnifiedGraphResp 类型加 `viz_building?: boolean`;`UnifiedKgStatus` 加 `viz_building?: boolean`。openKgView 拿到 `g.viz_building` → 图区显示居中提示「图谱索引构建中,首次构建大库可能数分钟…」+ 6s 轮询 refetch(仿 buildingScaleIndex effect:cancelled flag + 20min cap),建成自动换真图。
- [ ] Step 3 `cd frontend && npx tsc --noEmit` clean;`git diff | grep -c '^-.*[“”]'` = 0(弯引号校验)。
- [ ] Step 4 提交 `fix(frontend): 检索索引徽章与tier解耦可点构建 + 图谱页viz构建中轮询`。

## 收尾
- rebase 到 origin/master → push → `gh pr create --base master`(PR 说明带真机根因与 file:line)。
- 视觉验证留给用户(部署环境)。
