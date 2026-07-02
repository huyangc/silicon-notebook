# build_scale_index 分段计时观测 Plan

> REQUIRED SUB-SKILL: superpowers:subagent-driven-development。

**背景**:要决定 CLI `index` 阶段先做「向量 BLOB 化」还是「阶段间并行」,需要 49万库上各阶段真实耗时。给 `build_scale_index`(sqlite_repository.py:7128)加分段计时:事件走 `events` 通道(仿 process_source 的 pipeline stage 范式,L2324-2336),并把耗时汇总写进 manifest 便于事后 `cat manifest.json` 查看。

**Tech Stack**:纯观测,零行为变更。解释器 `/opt/homebrew/Caskroom/miniconda/base/bin/python`,测试在 worktree `backend/`。

## Global Constraints
- **零行为变更**:只加计时/事件/manifest 附加键;任何既有 manifest 键、返回值、异常语义不变。
- manifest 加性键 `build_ms`(dict)——`scale_index_status`/`load_scale_index` 只按名取键,加键安全;不得动 `version` 的取值或格式。
- 事件失败绝不破坏构建(EventLogger.emit 本身已吞异常,直接用)。

## Task 1: 分段计时 + manifest 汇总

**Files:** `backend/app/services/sqlite_repository.py`(仅 `build_scale_index` 函数体);Test 加到既有 scale index 测试文件(grep `build_scale_index` 找,如 test_scale_index*.py)。

- [ ] Step 1 写测试(先红):
  - 建小库(仿既有 build_scale_index 测试)→ `repo.build_scale_index(nb)` 返回的 manifest 含 `build_ms` dict,键恰为 `{"gather","transition","kg_matrix","chunk_matrix","viz_arrays","persist","total"}`,值为 int ≥0,且 `total >= max(各段)`。
  - monkeypatch `repo.event_log.emit` 收集事件:构建后收到 7 条 `kind=="scale_index_build"` 事件(6 段 + total),每条含 `notebook_id`/`stage`/`latency_ms`。
  - 既有 manifest 键(n_nodes/version/…)不受影响(旧断言继续绿)。
- [ ] Step 2 实现:
  - `build_scale_index` 开头建 `timings: dict[str,int]` 与局部 helper:
    ```python
    def _timed(stage_name, fn):
        t0 = time.perf_counter()
        out = fn()
        ms = round((time.perf_counter() - t0) * 1000)
        timings[stage_name] = ms
        self.event_log.emit({"kind": "scale_index_build", "notebook_id": notebook_id,
                             "stage": stage_name, "status": "done", "latency_ms": ms})
        return out
    ```
  - 六段包裹:`gather`=`_gather_kg_graph`;`transition`=`si.build_transition`;`kg_matrix`=knowledge_embeddings 的 `_vector_matrix`;`chunk_matrix`=chunk_embeddings 的 `_vector_matrix`;`viz_arrays`=`_build_viz_graph_arrays`;`persist`=末尾 `si.save_scale_index(...)` 调用(含 hnsw add_items+落盘)。
  - `total` 为全函数(gather 前起表),manifest 定义处加 `"build_ms": {**timings}`——注意 manifest 在 persist 之前构造、而 persist 的耗时要进 manifest:实现上把 `build_ms` dict 对象直接放进 manifest,persist 段计完再补 `timings["persist"]`/`timings["total"]`(同一 dict 引用,save 前先算 persist 无法……),**正确做法**:save_scale_index 接收 manifest 后才写盘——所以先跑 persist 计时再写?persist 本身就是写盘。取舍:`build_ms` 里 persist/total 在**事件里完整**,manifest 里的 persist 记「截至 manifest 构造时无法自含」——不接受。简单正确解:把 `si.save_scale_index` 拆两步不值得;改为 **manifest 写盘后**、事件照发 7 条,且**返回值 manifest dict**(内存中)补上 persist/total 两键(磁盘上的 manifest.json 少这两键,文档注明:磁盘 build_ms 含前 5 段,persist/total 看事件或返回值)。测试按此断言(磁盘 manifest 5 键,返回值 7 键)。
- [ ] Step 3 回归:`pytest tests/ -q -k "scale"` 后全量(基线 **1442 passed, 1 skipped**)。
- [ ] Step 4 提交 `feat(kg): build_scale_index 分段计时(events 事件+manifest build_ms),定位 index 阶段瓶颈`。

## 收尾
- rebase origin/master → push → `gh pr create --base master`。
