# 在线重建 scale 索引入口(base-tier)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 给 base-tier notebook 一个在线「重建检索索引」入口(后台任务),让场景A 大库增量摄取后不用切命令行也能刷新 scale 索引(CSR 图 + KG/chunk ANN + viz)。前后端同一 PR co-design。

**Architecture:** 后端加 `POST /notebooks/{id}/scale-index/rebuild`(base-tier 或已建过才允许,后台线程跑 `build_scale_index`,in-flight 去重)+ `GET /notebooks/{id}/scale-index/status`(exists/stale/building/计数)。前端在治理弹窗(仅 admin+base)加「重建检索索引」动作 + 状态行,复用现有 `buildingKg` 轮询范式:触发后轮询 status 到 `building=false && stale=false` 即完成。**重活仍是显式、后台、base-only**——不违背成本分离(不在请求线程内阻塞、不自动触发)。

**Tech Stack:** FastAPI + threading、pytest(后端);Next.js + TS(前端)。后端测试 `/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest`(worktree backend 下);前端 `cd frontend && npx tsc --noEmit`。

---

## File Structure

- `backend/app/services/sqlite_repository.py` — `scale_index_status` + `trigger_scale_index_rebuild`(+ `__init__` 加 building 守卫状态)。
- `backend/app/api/routes.py` — 两个端点。
- `backend/app/models/schemas.py` — `ScaleIndexStatus`。
- `backend/tests/test_scale_index_repo.py` — 后端测试。
- `frontend/app/page.tsx` — API 调用、治理弹窗动作、状态行、轮询 hook、state。

---

## Task 1: 后端端点 + repo 方法

**Files:** Modify `sqlite_repository.py`、`routes.py`、`schemas.py`;Test `tests/test_scale_index_repo.py`。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_scale_index_repo.py`(复用 `repo` fixture):
```python
def test_scale_index_status_and_rebuild(repo, monkeypatch):
    import json, time
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="base"))
    with repo._write() as db:
        now = "2026-07-01T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("s1", nb.id, "t", "md", "ready", now, now))
        for cid in ("c1", "c2"):
            db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (cid, nb.id, "s1", f"text {cid}", "", "[]", now))
            v = repo.embedder.embed_texts([cid])[0]
            db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                       (cid, nb.id, json.dumps(v), now))
    repo.rebuild_unified_kg(nb.id)

    # 非 base 且无索引 → 不合格
    st0 = repo.scale_index_status(nb.id)
    assert st0["exists"] is False and st0["eligible"] is False
    import pytest
    with pytest.raises(ValueError):
        repo.trigger_scale_index_rebuild(nb.id)

    # 标 base → 合格 → 触发后台重建 → 轮询到建成
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (nb.id,))
    assert repo.scale_index_status(nb.id)["eligible"] is True
    r = repo.trigger_scale_index_rebuild(nb.id)
    assert r["status"] in ("building", "already_building")
    for _ in range(50):
        if not repo.scale_index_status(nb.id)["building"]:
            break
        time.sleep(0.1)
    st = repo.scale_index_status(nb.id)
    assert st["exists"] is True and st["building"] is False and st["stale"] is False
    assert st["n_chunk_ann"] == 2 and st["has_chunk_ann"] is True
```
注:`build_scale_index` 依赖 chunk ANN 工件(前序 PR#130 分支已实现);本分支从 master 切出,**若 master 尚无 chunk ANN**,把 `n_chunk_ann==2`/`has_chunk_ann` 两断言放宽为 `>=0`/不判,并在汇报里指出依赖关系(本 feature 与 PR#130 独立,状态字段对 chunk_ann 只做透传)。

- [ ] **Step 2: 跑测试确认失败**

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index_repo.py::test_scale_index_status_and_rebuild -q`
预期 FAIL(`AttributeError: scale_index_status`)。

- [ ] **Step 3: `__init__` 加 building 守卫状态**

在 `SQLiteRepository.__init__`(约 L280–296,`self._write_lock = threading.RLock()` 附近)加:
```python
        self._scale_building: set = set()
        self._scale_building_lock = threading.Lock()
```

- [ ] **Step 4: repo 方法 `scale_index_status`**

放在 `build_scale_index` 附近:
```python
    def scale_index_status(self, notebook_id: str) -> dict:
        """scale 索引状态(供在线重建入口 UX)。exists=磁盘有 manifest;
        stale=manifest 版本 != 当前 _scale_index_version;building=后台重建中;
        eligible=base-tier 或已建过(镜像 CLI 门控)。计数取自 manifest。"""
        nb = self.get_notebook(notebook_id)  # KeyError → 404
        out_dir = os.path.join(self.settings.storage_dir, "kg_index", notebook_id)
        mpath = os.path.join(out_dir, "manifest.json")
        building = notebook_id in self._scale_building
        exists = os.path.exists(mpath)
        eligible = (nb.tier == "base") or exists
        if not exists:
            return {"exists": False, "stale": False, "building": building,
                    "eligible": eligible, "n_nodes": 0, "n_chunks": 0,
                    "n_ann": 0, "n_chunk_ann": 0, "has_chunk_ann": False}
        with open(mpath) as fh:
            manifest = json.load(fh)
        stale = manifest.get("version") != self._scale_index_version(notebook_id)
        return {"exists": True, "stale": bool(stale), "building": building,
                "eligible": True,
                "n_nodes": int(manifest.get("n_nodes", 0)),
                "n_chunks": int(manifest.get("n_chunks", 0)),
                "n_ann": int(manifest.get("n_ann", 0)),
                "n_chunk_ann": int(manifest.get("n_chunk_ann", 0)),
                "has_chunk_ann": bool(manifest.get("has_chunk_ann", False))}
```

- [ ] **Step 5: repo 方法 `trigger_scale_index_rebuild`**

```python
    def trigger_scale_index_rebuild(self, notebook_id: str) -> dict:
        """base-tier(或已建过)才允许;后台线程跑 build_scale_index,in-flight 去重。
        不合格 → ValueError(路由转 409)。build_scale_index 只读 DB 向量建 ANN,
        不发模型调用,故普通 daemon 线程即可(无需 copy_context)。"""
        nb = self.get_notebook(notebook_id)  # KeyError → 404
        out_dir = os.path.join(self.settings.storage_dir, "kg_index", notebook_id)
        eligible = (nb.tier == "base") or os.path.exists(os.path.join(out_dir, "manifest.json"))
        if not eligible:
            raise ValueError("notebook is not base-tier and has no existing scale index")
        with self._scale_building_lock:
            if notebook_id in self._scale_building:
                return {"status": "already_building", "notebook_id": notebook_id}
            self._scale_building.add(notebook_id)
        def _run():
            try:
                self.build_scale_index(notebook_id)
            except Exception:  # noqa: BLE001 — 后台任务,失败仅记录
                try:
                    self.event_log.logger.exception("build_scale_index failed for %s", notebook_id)
                except Exception:
                    pass
            finally:
                with self._scale_building_lock:
                    self._scale_building.discard(notebook_id)
        threading.Thread(target=_run, name=f"scaleidx-{notebook_id}", daemon=True).start()
        return {"status": "building", "notebook_id": notebook_id}
```
(确认文件顶部已 `import threading`/`import os`/`import json`;缺则补。)

- [ ] **Step 6: schema `ScaleIndexStatus`**

`schemas.py`(`UnifiedKgStatus` 附近):
```python
class ScaleIndexStatus(BaseModel):
    exists: bool
    stale: bool
    building: bool
    eligible: bool
    n_nodes: int = 0
    n_chunks: int = 0
    n_ann: int = 0
    n_chunk_ann: int = 0
    has_chunk_ann: bool = False
```

- [ ] **Step 7: 两个路由**

`routes.py`(`unified_kg_status`/`set_notebook_tier` 附近;`ScaleIndexStatus` 记得 import):
```python
@router.post("/notebooks/{notebook_id}/scale-index/rebuild", dependencies=[Depends(require_notebook_access)])
def rebuild_scale_index(notebook_id: str) -> dict:
    """在线重建 scale 检索索引(base-tier / 已建过;后台任务)。409 若不合格,404 若缺。"""
    try:
        return repository().trigger_scale_index_rebuild(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/notebooks/{notebook_id}/scale-index/status", dependencies=[Depends(require_notebook_access)])
def scale_index_status(notebook_id: str) -> ScaleIndexStatus:
    try:
        return ScaleIndexStatus(**repository().scale_index_status(notebook_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
```

- [ ] **Step 8: 跑测试 + 回归**

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index_repo.py tests/test_kg_search_api.py tests/test_unified_kg_api.py -q`
预期全 PASS。

- [ ] **Step 9: 提交**
```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/distracted-kirch-81bde2
git add backend/app/services/sqlite_repository.py backend/app/api/routes.py backend/app/models/schemas.py backend/tests/test_scale_index_repo.py
git commit -m "feat(scale): online scale-index rebuild endpoint + status (base-tier, background)"
```

---

## Task 2: 前端治理弹窗动作 + 状态行 + 轮询

**Files:** Modify `frontend/app/page.tsx`。

- [ ] **Step 1: API 调用 + 类型 + state**

在 page.tsx 现有 `rebuildKg` 定义(约 L601)附近加:
```ts
type ScaleIndexStatus = { exists: boolean; stale: boolean; building: boolean; eligible: boolean;
  n_nodes: number; n_chunks: number; n_ann: number; n_chunk_ann: number; has_chunk_ann: boolean };
const rebuildScaleIndex = (nb: string) => api<{ status: string; notebook_id: string }>(`/notebooks/${nb}/scale-index/rebuild`, { method: "POST" });
const fetchScaleIndexStatus = (nb: string) => api<ScaleIndexStatus>(`/notebooks/${nb}/scale-index/status`);
```
在 `buildingKg` state 声明附近加:
```ts
const [buildingScaleIndex, setBuildingScaleIndex] = useState(false);
const [scaleIndexStatus, setScaleIndexStatus] = useState<ScaleIndexStatus | null>(null);
```

- [ ] **Step 2: 载入 + 轮询状态**

新增 effect(镜像 `buildingKg` 轮询 L956–976),当选中 base notebook 时拉一次状态,重建期间每 6s 轮询到 `building=false`:
```ts
useEffect(() => {
  const nb = currentNotebookId;
  if (!nb || currentNotebook?.tier !== "base") { setScaleIndexStatus(null); return; }
  let cancelled = false;
  fetchScaleIndexStatus(nb).then(s => { if (!cancelled) setScaleIndexStatus(s); }).catch(() => {});
  return () => { cancelled = true; };
}, [currentNotebookId, currentNotebook?.tier]);

useEffect(() => {
  if (!buildingScaleIndex || !currentNotebookId) return;
  const nb = currentNotebookId; let cancelled = false;
  const poll = window.setInterval(async () => {
    try {
      const s = await fetchScaleIndexStatus(nb);
      if (cancelled) return;
      setScaleIndexStatus(s);
      if (!s.building) { setBuildingScaleIndex(false); setToast(s.stale ? "索引重建结束(仍有更新未纳入)" : "检索索引重建完成 ✓"); }
    } catch { /* transient */ }
  }, 6000);
  const cap = window.setTimeout(() => { if (!cancelled) { setBuildingScaleIndex(false); setToast("索引仍在后台构建,请稍后查看"); } }, 20 * 60 * 1000);
  return () => { cancelled = true; window.clearInterval(poll); window.clearTimeout(cap); };
}, [buildingScaleIndex, currentNotebookId]);
```

- [ ] **Step 3: 触发函数**

```ts
const startScaleIndexRebuild = async (nb: string) => {
  setBuildingScaleIndex(true);
  try {
    await rebuildScaleIndex(nb);
    setToast("已开始重建检索索引(后台进行,可能数分钟);完成后自动更新");
  } catch (e) { reportError(e); setBuildingScaleIndex(false); }
};
```

- [ ] **Step 4: 治理弹窗加动作(仅 admin + base)**

在治理弹窗 `actions` 数组(约 L2654–2662,紧接基准库那条之后)加:
```tsx
    ...((currentUser?.role === "admin" && currentNotebook?.tier === "base") ? [{
      label: buildingScaleIndex ? "检索索引重建中…" : "重建检索索引",
      desc: "重建大库的向量检索索引(CSR 图 + ANN),供 scale 检索使用;后台进行,完成后自动更新",
      action: () => { if (currentNotebookId && !buildingScaleIndex) startScaleIndexRebuild(currentNotebookId); },
    }] : []),
```

- [ ] **Step 5: 状态行(来源面板,base notebook)**

在来源面板 KG 状态显示附近(约 L2707–2756),base 且有 `scaleIndexStatus` 时加一行(复用 `.tool-hint`/`.tag` 风格):
```tsx
{currentNotebook?.tier === "base" && scaleIndexStatus && (
  <p className="tool-hint" style={{ margin: "2px 2px 8px" }}>
    检索索引:{scaleIndexStatus.building ? "构建中…" : !scaleIndexStatus.exists ? "未构建" : scaleIndexStatus.stale ? "已过期,建议重建" : "已同步"}
    {scaleIndexStatus.exists && ` · 节点 ${scaleIndexStatus.n_nodes} · chunk ${scaleIndexStatus.n_chunks}`}
  </p>
)}
```

- [ ] **Step 6: 类型检查 + 视觉验证**

`cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/distracted-kirch-81bde2/frontend && npx tsc --noEmit`
预期无错误。若能起前端预览,截图确认:base notebook 的治理弹窗出现「重建检索索引」、来源面板出现「检索索引:…」状态行、非 base/非 admin 不出现;点击后按钮变「重建中…」+ toast。无法起预览则至少保证 tsc clean 并在汇报里说明未做视觉验证。

- [ ] **Step 7: 提交**
```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/distracted-kirch-81bde2
git add frontend/app/page.tsx
git commit -m "feat(frontend): base-tier 重建检索索引 治理动作 + 状态行 + 轮询"
```

---

## Self-Review

- **Spec 覆盖**:后端(端点+status+守卫)Task 1、前端(动作+状态+轮询)Task 2,前后端同一 PR(co-design)。
- **成本分离**:重建是显式触发、后台线程、base-only、in-flight 去重——不阻塞请求、不自动跑、不碰 active。
- **不变量/一致性**:`ScaleIndexStatus` 字段在 schema/repo/前端 TS 三处一致;轮询复用 `buildingKg` 既有范式(6s + 20min cap);动作门控 admin+base 与 tier 治理一致。
- **依赖**:`n_chunk_ann`/`has_chunk_ann` 透传自 manifest;若本分支 base(master)尚无 chunk ANN(PR#130 未合),这两字段恒 0/false,不影响功能(状态行只展示,不依赖)。
- **权限**:两端点用 `require_notebook_access`(与其它 KG 端点一致);写动作前端再叠 admin+base 门控。**注意**:`require_notebook_access` 当前不分读写(见 memory),但与既有 kg/rebuild、tier 端点同守卫,一致即可。
```
