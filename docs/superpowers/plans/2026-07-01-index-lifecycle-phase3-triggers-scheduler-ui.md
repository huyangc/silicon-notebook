# Phase 3：触发时机 + 低峰调度 + 前端四态 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development。Steps use checkbox。

**Goal:** 把索引生命周期缝成运维闭环:系统按规模自动 surface「需(重)建」,用户选 **立即 / 空闲时**,空闲=低峰窗口调度器自动跑;「重建」自动挑 fold(有索引且 delta 小)或全量(无索引/质量维护)。前后端同 PR(累加 #143)。

**Architecture:** 后端扩 `trigger_scale_index_rebuild(nb, when, mode)`:`when="now"` 立即后台、`when="idle"` 入 `_scale_idle_queue` 队列;`mode="auto"` = 有(含 stale)索引→`fold_scale_index_delta` 否则→`build_scale_index`。懒启动 daemon 调度器,低峰窗口(config)内 drain 队列。状态机加 `queued`。前端治理弹窗「重建检索索引」弹二选(立即/空闲),状态行展示四态。

**Tech Stack:** FastAPI/threading/datetime、Next.js/TS、pytest。解释器 `/opt/homebrew/Caskroom/miniconda/base/bin/python`。

## Global Constraints

- 依据 [spec §3/§6](../specs/2026-07-01-index-lifecycle-redesign.md)。**重建必然发生,用户只挑时机**(now/idle)。
- 复用 #134 的 `_scale_building`/`trigger_scale_index_rebuild` + Phase 2 `fold_scale_index_delta`。
- 调度器**懒启动**(首次 idle 入队时启一个 daemon),避免 app-startup 接线;`_process_idle_queue(force=False)` 可单测(force 绕过窗口)。
- 前端复用 #134 已有 `rebuildScaleIndex`/`fetchScaleIndexStatus`/`buildingScaleIndex` 轮询 + `.tool-hint`;弯引号约定(见 memory)。

---

## File Structure
- `backend/app/core/config.py` — 低峰窗口 + 调度轮询配置。
- `backend/app/services/sqlite_repository.py` — trigger when/mode + `_run_scale_op` + `_scale_idle_queue` + `_process_idle_queue` + 懒调度器 + status `queued`。
- `backend/app/api/routes.py` — rebuild 端点接收 `{when, mode}`。
- `backend/tests/test_scale_index_repo.py` — 后端测试。
- `frontend/app/page.tsx` — now/idle 二选 + 四态。

---

## Task 1: 后端 trigger when/mode + fold/full 分派 + idle 队列

**Files:** Modify `config.py`、`sqlite_repository.py`、`routes.py`;Test `test_scale_index_repo.py`。

**Interfaces:**
- Produces: `trigger_scale_index_rebuild(nb, when="now", mode="auto") -> {status, notebook_id}`（status ∈ building|queued|already_building）;`_run_scale_op(nb, mode)`(guarded,跑 fold 或 full);`scale_index_status` 增 `queued` state。

- [ ] **Step 1: 写失败测试**
```python
def test_trigger_when_and_mode(repo, monkeypatch):
    from app.models.schemas import NotebookCreate
    import json
    nb = repo.create_notebook(NotebookCreate(name="base"))
    with repo._write() as db:
        now="2026-07-01T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",("s1",nb.id,"t","md","ready",now,now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) VALUES (?,?,?,?,?,?,?)",("c1",nb.id,"s1","x","","[]",now))
        v=repo.embedder.embed_texts(["c1"])[0]
        db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",("c1",nb.id,json.dumps(v),now))
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?",(nb.id,))
    repo.rebuild_unified_kg(nb.id)
    # when=idle → 入队、status=queued、不立即建
    r = repo.trigger_scale_index_rebuild(nb.id, when="idle")
    assert r["status"] == "queued"
    assert repo.scale_index_status(nb.id)["state"] == "queued"
    assert nb.id in repo._scale_idle_queue
    # force drain(绕过时间窗)→ 建成、出队、state 回 indexed
    repo._process_idle_queue(force=True)
    import time
    for _ in range(50):
        if not repo._scale_building and nb.id not in repo._scale_idle_queue: break
        time.sleep(0.1)
    assert nb.id not in repo._scale_idle_queue
    assert repo.scale_index_status(nb.id)["exists"] is True
```

- [ ] **Step 2: 跑测试确认失败**
`… -m pytest tests/test_scale_index_repo.py::test_trigger_when_and_mode -q` → FAIL(trigger 不接受 when / 无 `_scale_idle_queue`)。

- [ ] **Step 3: config 加窗口**
```python
    scale_index_offpeak_start_hour: int = Field(2, env="SCALE_INDEX_OFFPEAK_START_HOUR")   # 低峰窗口起(含)
    scale_index_offpeak_end_hour: int = Field(6, env="SCALE_INDEX_OFFPEAK_END_HOUR")        # 低峰窗口止(不含);start>end 视为跨零点
    scale_index_scheduler_poll_seconds: int = Field(300, env="SCALE_INDEX_SCHEDULER_POLL_SECONDS")
```

- [ ] **Step 4: `__init__` 加队列**（`_scale_building` 附近）
```python
        self._scale_idle_queue: dict = {}   # {notebook_id: mode} 待低峰重建
        self._scale_scheduler_started = False
```

- [ ] **Step 5: 抽 `_run_scale_op` + 改 trigger**

新增 `_run_scale_op(self, notebook_id, mode)`(把现有 `_run` 的 guarded 后台执行抽出,按 mode 选 fold/full):
```python
    def _resolve_scale_mode(self, notebook_id, mode):
        if mode in ("fold", "full"):
            return mode
        # auto:有(含 stale)索引 → fold,否则 full
        return "fold" if self._scale_index(notebook_id, allow_stale=True) is not None else "full"

    def _run_scale_op(self, notebook_id, mode):
        """后台执行(guarded):按 mode 跑 fold_scale_index_delta 或 build_scale_index。"""
        with self._scale_building_lock:
            if notebook_id in self._scale_building:
                return
            self._scale_building.add(notebook_id)
        def _run():
            try:
                op = self._resolve_scale_mode(notebook_id, mode)
                if op == "fold":
                    self.fold_scale_index_delta(notebook_id)   # 注:该方法内部也会 _scale_building 去重;见下
                else:
                    self.build_scale_index(notebook_id)
            except Exception:
                try: self.event_log.logger.exception("scale op failed for %s", notebook_id)
                except Exception: pass
            finally:
                with self._scale_building_lock:
                    self._scale_building.discard(notebook_id)
        threading.Thread(target=_run, name=f"scaleidx-{notebook_id}", daemon=True).start()
```
**注意去重嵌套**:`fold_scale_index_delta`(Phase 2)自身会 `_scale_building.add`。若 `_run_scale_op` 也 add,fold 内部会因已在集合而返回 `already_building` 空跑。**解法**:`_run_scale_op` 不自己 add/discard,改为直接调用(让 fold/build 各自的 guard 生效)——即 `_run` 里直接 `self.fold_scale_index_delta(...)`/`self.build_scale_index(...)`,删掉 `_run_scale_op` 外层的 add/discard。build_scale_index 目前无自身 guard,故:统一让 `_run_scale_op` 负责 guard,而 fold 被调用时**跳过自身 guard**——给 `fold_scale_index_delta` 加参数 `_guarded=False` 时不 add(内部已在 building)。实现者择一干净方案并在汇报说明;推荐:`_run_scale_op` 持 guard,fold/build 都提供「假定已 guarded」的内部入口(fold 加 `_assume_locked=False` 参数,True 时跳过自身 add/discard)。

改 `trigger_scale_index_rebuild`:
```python
    def trigger_scale_index_rebuild(self, notebook_id, when="now", mode="auto"):
        nb = self.get_notebook(notebook_id)
        out_dir = os.path.join(self.settings.storage_dir, "kg_index", notebook_id)
        eligible = (nb.tier == "base") or os.path.exists(os.path.join(out_dir, "manifest.json"))
        if not eligible:
            raise ValueError("notebook is not base-tier and has no existing scale index")
        if when == "idle":
            with self._scale_building_lock:
                self._scale_idle_queue[notebook_id] = mode
            self._ensure_scale_scheduler()
            return {"status": "queued", "notebook_id": notebook_id}
        if notebook_id in self._scale_building:
            return {"status": "already_building", "notebook_id": notebook_id}
        self._run_scale_op(notebook_id, mode)
        return {"status": "building", "notebook_id": notebook_id}
```

- [ ] **Step 6: `_process_idle_queue` + 懒调度器**
```python
    def _process_idle_queue(self, force=False):
        """低峰窗口(或 force)内 drain idle 队列,逐个后台重建。"""
        import datetime
        if not force:
            hour = datetime.datetime.now().hour
            lo, hi = self.settings.scale_index_offpeak_start_hour, self.settings.scale_index_offpeak_end_hour
            in_window = (lo <= hour < hi) if lo <= hi else (hour >= lo or hour < hi)
            if not in_window:
                return
        with self._scale_building_lock:
            queued = dict(self._scale_idle_queue); self._scale_idle_queue.clear()
        for nb, mode in queued.items():
            self._run_scale_op(nb, mode)

    def _ensure_scale_scheduler(self):
        import time
        with self._scale_building_lock:
            if self._scale_scheduler_started:
                return
            self._scale_scheduler_started = True
        def _loop():
            while True:
                time.sleep(max(30, self.settings.scale_index_scheduler_poll_seconds))
                try:
                    self._process_idle_queue(force=False)
                except Exception:
                    try: self.event_log.logger.exception("scale scheduler tick failed")
                    except Exception: pass
        threading.Thread(target=_loop, name="scaleidx-scheduler", daemon=True).start()
```

- [ ] **Step 7: status 加 `queued`** —— `scale_index_status` 里 building 判定之前(或之后)加:若 `notebook_id in self._scale_idle_queue` → `state="queued"`(优先级:building > queued > 其余)。

- [ ] **Step 8: 路由接 body** —— `routes.py` 的 `rebuild_scale_index` 接 `RebuildScaleIndexRequest{when: str = "now", mode: str = "auto"}`(新 schema),传给 trigger。校验 when∈{now,idle}、mode∈{auto,fold,full},非法 400。

- [ ] **Step 9: 跑测试 + 回归** —— `test_scale_index_repo.py test_kg_search_api.py` 全绿。
- [ ] **Step 10: 提交** —— 4 文件(config/sqlite_repository/routes/schemas + test)。commit `feat(scale): rebuild timing (now/idle) + off-peak scheduler + fold/full auto`。

---

## Task 2: 前端 now/idle 二选 + 四态

**Files:** Modify `frontend/app/page.tsx`。

- [ ] **Step 1**（无独立测试,tsc + 视觉）:
  - `rebuildScaleIndex(nb, when)` 改为带 body `{when}`:`api(..., {method:"POST", body: JSON.stringify({when})})`。
  - `ScaleIndexStatus` 类型加 `state` 已在(#140);治理弹窗「重建检索索引」动作改为弹**两个子动作**:「立即重建」(when="now")/「空闲时重建」(when="idle"),复用现有 InfoModal actions 二级或直接两个按钮。
  - 状态行按 `scaleIndexStatus.state` 展示:`unindexed→未索引`/`suggested→建议建索引`/`queued→已排队(空闲时建)`/`building→构建中…`/`indexed→已同步`/`stale→建议重建`。
  - 触发 idle 后 toast「已排队,将在服务器空闲时重建」;now 后沿用现有「后台进行…」。
- [ ] **Step 2**：`cd frontend && npx tsc --noEmit` clean;弯引号 grep=0;能起预览则截图四态(admin+base),起不了则 tsc 兜底+说明未视觉验证。
- [ ] **Step 3**：提交 `frontend/app/page.tsx`。commit `feat(frontend): scale-index rebuild now/idle choice + four-state status`。

---

## Self-Review
- **Spec 覆盖**:§3 触发(auto surface 靠既有 status state)+ §6 now/idle 时机 + 低峰调度 + fold/full auto。
- **保守/复用**:trigger 默认 when="now" 兼容既有调用;调度器懒启动不影响未用场景;`_process_idle_queue(force)` 可测。
- **去重一致**:`_run_scale_op`/fold/build 的 `_scale_building` guard 不嵌套空跑(实现者按 Step 5 注选干净方案)。
- **前后端同 PR**(co-design)。
- **已知**:idle 队列 in-memory,进程重启丢失但 notebook 仍 stale→可重新触发(可接受 v1);调度器用本地时区 `datetime.now()`。
