# Phase 1a：delta 基础设施 + 索引状态机字段 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax。

**Goal:** 给 scale 索引加「水位线 + delta 计算 + 状态机」,为「索引核 ⊕ 暴力 delta」统一分派(Phase 1b)与两档合并(Phase 2)打地基。本 Phase **不改检索行为**,只加基础设施与更丰富的状态上报。

**Architecture:** `build_scale_index` 的 manifest 记一个**水位线**=build 时纳入的 `source_id` 集合;新 `_index_delta(nb)` 按水位算出「水位后新增的 source/chunk」;`scale_index_status` 扩展出 `state` 枚举 + delta 计数。全部纯增量,旧索引/旧调用向后兼容。

**Tech Stack:** SQLite、Python、pytest。解释器 `/opt/homebrew/Caskroom/miniconda/base/bin/python`;测试在 worktree 的 `backend/` 下跑。

## Global Constraints

- 设计依据 [docs/superpowers/specs/2026-07-01-index-lifecycle-redesign.md](../specs/2026-07-01-index-lifecycle-redesign.md)。
- **向后兼容**:旧磁盘索引(manifest 无 `watermark_sources`)→ delta 视为「全部 source 都是 delta」或 0,不得抛错。
- **不改检索**:本 Phase 不动 `_retrieve_*`/`scale_ppr`/`federated_retrieve`。
- 水位用 **source_id 集合**(source 数远小于 chunk 数,即便大库也就千级),不用秒级时间戳(规避 P1-9 同秒 stale-cache 风险)。

---

## File Structure

- `backend/app/services/sqlite_repository.py` — `build_scale_index`(manifest 加水位)、新 `_index_delta`、`scale_index_status`(加 state+delta)。
- `backend/app/core/config.py` — 阈值配置。
- `backend/app/models/schemas.py` — `ScaleIndexStatus` 加字段。
- `backend/tests/test_scale_index_repo.py` — 测试。

---

## Task 1: manifest 记水位线（build 时纳入的 source 集合）

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（`build_scale_index` 的 manifest 构造处，约 L6300–6320）
- Test: `backend/tests/test_scale_index_repo.py`

**Interfaces:**
- Produces: `build_scale_index` 写出的 manifest 新增键 `"watermark_sources": List[str]`（排序后的 source_id;该 notebook build 时所有 source）。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_scale_index_repo.py`（复用 `repo` fixture）：
```python
def test_build_scale_index_records_watermark_sources(repo):
    import os, json
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="base"))
    with repo._write() as db:
        now = "2026-07-01T00:00:00"
        for sid in ("s1", "s2"):
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (sid, nb.id, "t", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("c1", nb.id, "s1", "x", "", "[]", now))
        v = repo.embedder.embed_texts(["c1"])[0]
        db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                   ("c1", nb.id, json.dumps(v), now))
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    mpath = os.path.join(repo.settings.storage_dir, "kg_index", nb.id, "manifest.json")
    with open(mpath) as fh:
        manifest = json.load(fh)
    assert sorted(manifest["watermark_sources"]) == ["s1", "s2"]
```

- [ ] **Step 2: 跑测试确认失败**

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index_repo.py::test_build_scale_index_records_watermark_sources -q`
预期 FAIL（`KeyError: 'watermark_sources'`）。

- [ ] **Step 3: 实现**

在 `build_scale_index` 里，构造 `manifest = {...}` 之前，查该 notebook 全部 source_id：
```python
        with self._connect() as db:
            watermark_sources = sorted(
                r["id"] for r in db.execute(
                    "SELECT id FROM sources WHERE notebook_id=?", (notebook_id,)).fetchall())
```
并在 `manifest = { ... }` 字典里加一行：
```python
            "watermark_sources": watermark_sources,
```
（放在现有 `"n_viz_edges": ...` 后即可。`save_scale_index` 原样把 manifest 落盘，无需改。）

- [ ] **Step 4: 跑测试确认通过 + 回归**

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index_repo.py -q`
预期全 PASS（含新测试；`test_build_scale_index_writes_artifacts` 等既有仍绿）。

- [ ] **Step 5: 提交**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/distracted-kirch-81bde2
git add backend/app/services/sqlite_repository.py backend/tests/test_scale_index_repo.py
git commit -m "feat(scale): record watermark_sources in scale-index manifest"
```

---

## Task 2: `_index_delta(nb)` — 按水位算 delta

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（新方法，放在 `scale_index_status` 附近）
- Test: `backend/tests/test_scale_index_repo.py`

**Interfaces:**
- Consumes: manifest 的 `watermark_sources`（Task 1）。
- Produces: `_index_delta(self, notebook_id: str) -> dict`，返回
  `{"delta_sources": List[str], "delta_chunks": int, "indexed": bool}`。
  `indexed=False`（无 manifest）时 `delta_sources`=当前全部 source、`delta_chunks`=当前全部 chunk（即「全是 delta」，与「未索引=纯暴力」语义一致）。

- [ ] **Step 1: 写失败测试**

```python
def test_index_delta_after_new_source(repo):
    import json
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="base"))
    with repo._write() as db:
        now = "2026-07-01T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("s1", nb.id, "t", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("c1", nb.id, "s1", "x", "", "[]", now))
        v = repo.embedder.embed_texts(["c1"])[0]
        db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                   ("c1", nb.id, json.dumps(v), now))
    # 未索引 → 全是 delta
    d0 = repo._index_delta(nb.id)
    assert d0["indexed"] is False and d0["delta_chunks"] == 1 and d0["delta_sources"] == ["s1"]
    # 建索引 → delta 清零
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    d1 = repo._index_delta(nb.id)
    assert d1["indexed"] is True and d1["delta_chunks"] == 0 and d1["delta_sources"] == []
    # 新增一个 source+chunk → delta=1
    with repo._write() as db:
        now2 = "2026-07-02T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("s2", nb.id, "t", "md", "ready", now2, now2))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("c2", nb.id, "s2", "y", "", "[]", now2))
    d2 = repo._index_delta(nb.id)
    assert d2["indexed"] is True and d2["delta_sources"] == ["s2"] and d2["delta_chunks"] == 1
```

- [ ] **Step 2: 跑测试确认失败**

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index_repo.py::test_index_delta_after_new_source -q`
预期 FAIL（`AttributeError: _index_delta`）。

- [ ] **Step 3: 实现**

`scale_index_status` 之前新增：
```python
    def _index_delta(self, notebook_id: str) -> dict:
        """按 manifest 水位算 delta:水位后新增的 source 及其 chunk 数。
        无 manifest(未索引)→ 全部 source/chunk 视为 delta(语义:纯暴力)。"""
        out_dir = os.path.join(self.settings.storage_dir, "kg_index", notebook_id)
        mpath = os.path.join(out_dir, "manifest.json")
        with self._connect() as db:
            cur_sources = [r["id"] for r in db.execute(
                "SELECT id FROM sources WHERE notebook_id=?", (notebook_id,)).fetchall()]
            if not os.path.exists(mpath):
                nchunks = db.execute(
                    "SELECT COUNT(*) c FROM chunks WHERE notebook_id=?", (notebook_id,)).fetchone()["c"]
                return {"delta_sources": sorted(cur_sources),
                        "delta_chunks": int(nchunks), "indexed": False}
            with open(mpath) as fh:
                watermark = set(json.load(fh).get("watermark_sources", []))
            delta_sources = sorted(s for s in cur_sources if s not in watermark)
            if not delta_sources:
                return {"delta_sources": [], "delta_chunks": 0, "indexed": True}
            ph = ",".join("?" for _ in delta_sources)
            nchunks = db.execute(
                f"SELECT COUNT(*) c FROM chunks WHERE notebook_id=? AND source_id IN ({ph})",
                (notebook_id, *delta_sources)).fetchone()["c"]
        return {"delta_sources": delta_sources, "delta_chunks": int(nchunks), "indexed": True}
```

- [ ] **Step 4: 跑测试确认通过**

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index_repo.py::test_index_delta_after_new_source -q`
预期 PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_scale_index_repo.py
git commit -m "feat(scale): _index_delta computes post-watermark source/chunk delta"
```

---

## Task 3: `scale_index_status` 扩展 state 枚举 + delta 计数

**Files:**
- Modify: `backend/app/core/config.py`（阈值）、`backend/app/services/sqlite_repository.py`（`scale_index_status`）、`backend/app/models/schemas.py`（`ScaleIndexStatus`）
- Test: `backend/tests/test_scale_index_repo.py`

**Interfaces:**
- Consumes: `_index_delta`（Task 2）、settings 阈值（本 Task）。
- Produces: `scale_index_status` 返回 dict 新增 `"state": str`（`unindexed|suggested|building|indexed|stale`）、`"delta_chunks": int`、`"total_chunks": int`;`ScaleIndexStatus` schema 同步加这三字段。

- [ ] **Step 1: 加阈值配置**

`config.py`（`chunk_ann_enabled` 附近）：
```python
    index_suggest_chunk_threshold: int = Field(2000, env="INDEX_SUGGEST_CHUNK_THRESHOLD")  # 未索引库总 chunk 超此 → 建议建索引
    index_stale_delta_threshold: int = Field(500, env="INDEX_STALE_DELTA_THRESHOLD")        # 已索引库 delta chunk 超此 → 建议重建
```

- [ ] **Step 2: 写失败测试**

```python
def test_scale_index_status_state_machine(repo, monkeypatch):
    import json
    from app.models.schemas import NotebookCreate
    monkeypatch.setattr(repo.settings, "index_suggest_chunk_threshold", 3)
    monkeypatch.setattr(repo.settings, "index_stale_delta_threshold", 1)
    nb = repo.create_notebook(NotebookCreate(name="base"))
    def add_source(sid, cids, day):
        with repo._write() as db:
            now = f"2026-07-{day:02d}T00:00:00"
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (sid, nb.id, "t", "md", "ready", now, now))
            for cid in cids:
                db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                           "VALUES (?,?,?,?,?,?,?)", (cid, nb.id, sid, "x", "", "[]", now))
                v = repo.embedder.embed_texts([cid])[0]
                db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                           (cid, nb.id, json.dumps(v), now))
    # 小库 → unindexed
    add_source("s1", ["c1"], 1)
    assert repo.scale_index_status(nb.id)["state"] == "unindexed"
    # 越过建议阈值(3) → suggested
    add_source("s2", ["c2", "c3", "c4"], 2)
    assert repo.scale_index_status(nb.id)["state"] == "suggested"
    # 建索引 → indexed, delta=0
    repo.rebuild_unified_kg(nb.id); repo.build_scale_index(nb.id)
    st = repo.scale_index_status(nb.id)
    assert st["state"] == "indexed" and st["delta_chunks"] == 0
    # 新增 delta 超阈值(1) → stale
    add_source("s3", ["c5", "c6"], 3)
    st2 = repo.scale_index_status(nb.id)
    assert st2["state"] == "stale" and st2["delta_chunks"] == 2
```

- [ ] **Step 3: 跑测试确认失败**

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index_repo.py::test_scale_index_status_state_machine -q`
预期 FAIL（`state` 键不存在）。

- [ ] **Step 4: 实现 status 扩展**

改 `scale_index_status`，在原返回上补 `state`/`delta_chunks`/`total_chunks`。用 `_index_delta` + 现有 `building`/`exists`/`stale`(版本失配) + 阈值：
```python
    def scale_index_status(self, notebook_id: str) -> dict:
        nb = self.get_notebook(notebook_id)
        out_dir = os.path.join(self.settings.storage_dir, "kg_index", notebook_id)
        mpath = os.path.join(out_dir, "manifest.json")
        building = notebook_id in self._scale_building
        exists = os.path.exists(mpath)
        delta = self._index_delta(notebook_id)
        with self._connect() as db:
            total_chunks = db.execute(
                "SELECT COUNT(*) c FROM chunks WHERE notebook_id=?", (notebook_id,)).fetchone()["c"]
        eligible = (nb.tier == "base") or exists
        base = {"exists": exists, "building": building, "eligible": eligible,
                "delta_chunks": int(delta["delta_chunks"]), "total_chunks": int(total_chunks)}
        if building:
            base["state"] = "building"
        elif not exists:
            base["state"] = "suggested" if total_chunks > self.settings.index_suggest_chunk_threshold else "unindexed"
        else:
            with open(mpath) as fh:
                manifest = json.load(fh)
            version_stale = manifest.get("version") != self._scale_index_version(notebook_id)
            delta_over = delta["delta_chunks"] > self.settings.index_stale_delta_threshold
            base["state"] = "stale" if (version_stale or delta_over) else "indexed"
            base.update({
                "stale": bool(version_stale or delta_over),
                "n_nodes": int(manifest.get("n_nodes", 0)),
                "n_chunks": int(manifest.get("n_chunks", 0)),
                "n_ann": int(manifest.get("n_ann", 0)),
                "n_chunk_ann": int(manifest.get("n_chunk_ann", 0)),
                "has_chunk_ann": bool(manifest.get("has_chunk_ann", False))})
            return base
        # 未建/构建中:补齐既有字段的默认值(保持 schema 稳定)
        base.update({"stale": False, "n_nodes": 0, "n_chunks": 0, "n_ann": 0,
                     "n_chunk_ann": 0, "has_chunk_ann": False})
        return base
```

- [ ] **Step 5: schema 加字段**

`schemas.py` 的 `ScaleIndexStatus` 加：
```python
    state: str = "unindexed"
    delta_chunks: int = 0
    total_chunks: int = 0
```
（放在现有字段后;均有默认值，向后兼容。）

- [ ] **Step 6: 跑测试 + 回归**

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index_repo.py tests/test_kg_search_api.py tests/test_unified_kg_api.py -q`
预期全 PASS（含新测试 + PR#134 的 `test_scale_index_status_and_rebuild` 仍绿——注意它可能断言了旧字段，若断言 `st["stale"]` 等仍成立即可）。

- [ ] **Step 7: 提交**

```bash
git add backend/app/core/config.py backend/app/services/sqlite_repository.py backend/app/models/schemas.py backend/tests/test_scale_index_repo.py
git commit -m "feat(scale): scale_index_status reports state machine + delta counts"
```

---

## Self-Review

- **Spec 覆盖**：本 Phase 覆盖 spec §4 的「delta 水位」+ §3 状态机的**上报**（不含转移动作）+ §8 的 `_index_delta`/status 扩展。⊕ 分派、P0-00、fold、调度、UI 明确留给 Phase 1b/2/3。
- **向后兼容**：旧 manifest 无 `watermark_sources` → `_index_delta` 走「全是 delta」分支不抛错；status 新字段均有默认值。
- **不改检索**：本 Phase 不触 `_retrieve_*`/`scale_ppr`/`federated_retrieve`；纯加基础设施 + 状态上报。
- **类型一致**：`_index_delta` 返回 `{delta_sources,delta_chunks,indexed}` 在 Task 2 定义、Task 3 消费一致；`scale_index_status` 新增 `state/delta_chunks/total_chunks` 与 schema 三字段一致；`watermark_sources` 在 Task 1 写、Task 2 读一致。
- **水位用 source_id 集合**：规避秒级时间戳 stale 风险（spec Global Constraint）。

---

## 后续（不在本计划内）

- **Phase 1b**：`_retrieve_chunks`/`_retrieve_scored`/`federated_retrieve`/`scale_ppr(修 P0-00)` 改「索引核 ⊕ 暴力 delta」分派，消费本 Phase 的 `_index_delta`。
- **Phase 2**：`fold_scale_index_delta` 增量 fold + hnsw handle 缓存。
- **Phase 3**：摄取后阈值自动 surface + now/idle 二选 + 低峰调度器 + 前端四态。
