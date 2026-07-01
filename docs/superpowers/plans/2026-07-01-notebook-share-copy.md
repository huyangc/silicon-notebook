# Notebook 分享与拷贝(Phase 1)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户分享自己的 notebook,他人凭分享码把**小库**深拷贝到自己的空间(独立副本)。

**Architecture:** `notebooks` 加 `is_shared`/`share_token` 两列;4 个端点(share/unshare/preview/copy);拷贝在单事务内做全表 id 重映射(列级 + JSON 内嵌)+ 磁盘文件复制 + 完整性自检。大库拷贝接口 409 拒绝(只读共享留 Phase 2)。**不碰 owner 隔离层**(拷贝=新建独立自有库)。

**Tech Stack:** FastAPI + 同步路由;`SQLiteRepository`(`app/services/sqlite_repository.py`);pydantic-settings v2;pytest。

**Spec:** `docs/superpowers/specs/2026-07-01-notebook-share-and-copy-design.md`

**测试解释器:** `/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest`(从 `backend/` 跑)。全程保持全量绿。

**新测试文件:** `backend/tests/test_notebook_share_copy.py`(本计划所有测试都放这)。

---

## Task 1: DB 迁移 —— notebooks 加 is_shared / share_token

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(schema 迁移段,`notebooks.tier` 迁移附近,约 726-731 行)
- Test: `backend/tests/test_notebook_share_copy.py`

- [ ] **Step 1: 写失败测试**

> **测试脚手架(本文件顶部,一次性,后续 Task 复用)**:`repo` 与 `client` 两个 fixture 都按同一 `tmp_path` 设 env → 指向**同一个 tmp DB 文件**(`repo` 用于种数据,`client` 驱动 API)。conftest.py 已 autouse 每测清 `get_settings`/`deps.repository` 的 lru_cache,故 `client` 用 `from app.main import app` 即可(路由每请求重建 `repository()` 读当前 env)。`SILICON_NOTEBOOK_AUTH_OPTIONAL=true` 让无 token 请求回退 seeded admin(id=`user-local`),故种库时 `owner="user-local"` = API 调用者。

```python
# backend/tests/test_notebook_share_copy.py
import json
import uuid
import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, _now


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "true")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    return SQLiteRepository(Settings())


@pytest.fixture
def client(tmp_path, monkeypatch):
    _env(monkeypatch, tmp_path)
    from app.main import app
    return TestClient(app)


def _mk_nb(repo, name="NB", owner="user-local"):
    """直接建一个空 notebook(不依赖当前用户 ContextVar),返回 nb_id。"""
    nb_id = f"nb-{uuid.uuid4().hex[:10]}"; now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebooks (id,name,purpose,primary_domain,status,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)", (nb_id, name, "", "Semiconductor", "draft", owner, now, now))
    return nb_id


def _rows(repo, table, nb):
    with repo._connect() as db:
        return db.execute(f"SELECT * FROM {table} WHERE notebook_id=?", (nb,)).fetchall()


def test_notebooks_has_share_columns(repo):
    with repo._connect() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(notebooks)")}
    assert "is_shared" in cols
    assert "share_token" in cols
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_notebook_share_copy.py::test_notebooks_has_share_columns -v`
Expected: FAIL(`is_shared`/`share_token` 不在列里)

- [ ] **Step 3: 加迁移**

在 `sqlite_repository.py` 的 notebooks 列迁移段(紧邻现有 `tier` 迁移,约 729-731 行之后)加:

```python
            if "is_shared" not in nb_cols:
                db.execute("ALTER TABLE notebooks ADD COLUMN is_shared INTEGER NOT NULL DEFAULT 0")
            if "share_token" not in nb_cols:
                db.execute("ALTER TABLE notebooks ADD COLUMN share_token TEXT DEFAULT NULL")
                db.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_notebooks_share_token "
                    "ON notebooks(share_token) WHERE share_token IS NOT NULL"
                )
```

> `nb_cols` 是现有 tier 迁移已算好的 `{r["name"] for r in db.execute("PRAGMA table_info(notebooks)")}`。若该变量作用域不覆盖此处,就地重新读一次 `PRAGMA table_info(notebooks)`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_notebook_share_copy.py::test_notebooks_has_share_columns -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_notebook_share_copy.py
git commit -m "feat(share): notebooks 加 is_shared/share_token 列 + 唯一索引"
```

---

## Task 2: Settings —— 拷贝大小阈值(可配)

**Files:**
- Modify: `backend/app/core/config.py`(与其它 `validation_alias` 字段并列)
- Test: `backend/tests/test_notebook_share_copy.py`

- [ ] **Step 1: 写失败测试**

```python
def test_copy_thresholds_defaults():
    from app.core.config import Settings
    s = Settings()
    assert s.notebook_copy_max_bytes == 50 * 1024 * 1024
    assert s.notebook_copy_max_rows == 5000
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_notebook_share_copy.py::test_copy_thresholds_defaults -v`
Expected: FAIL(`AttributeError`)

- [ ] **Step 3: 加 Settings 字段**

在 `config.py` 加(用 `validation_alias`,见 [[pydantic-env-alias-gotcha]]——`Field(env=)` 在本项目失效):

```python
    notebook_copy_max_bytes: int = Field(50 * 1024 * 1024, validation_alias="NOTEBOOK_COPY_MAX_BYTES")
    notebook_copy_max_rows: int = Field(5000, validation_alias="NOTEBOOK_COPY_MAX_ROWS")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_notebook_share_copy.py::test_copy_thresholds_defaults -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/core/config.py backend/tests/test_notebook_share_copy.py
git commit -m "feat(share): 加拷贝大小阈值 Settings(可配,默认 50MB/5000 行)"
```

---

## Task 3: 仓库层 —— share / unshare / size 统计 / 按 token 查

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(新方法,建议放 `create_notebook`/`get_notebook` 附近)
- Test: `backend/tests/test_notebook_share_copy.py`

- [ ] **Step 1: 写失败测试**

```python
def test_share_sets_token_idempotent_then_unshare_clears(repo):
    nb = _mk_nb(repo, "L")
    out = repo.share_notebook(nb)
    assert out["share_token"].startswith("shr-")
    assert repo.find_notebook_by_share_token(out["share_token"]) == nb
    # 幂等:再分享返回同一个 token
    assert repo.share_notebook(nb)["share_token"] == out["share_token"]
    # 取消 → token 失效
    repo.unshare_notebook(nb)
    assert repo.find_notebook_by_share_token(out["share_token"]) is None


def test_copy_stats_reports_size_and_copyable(repo):
    nb = _mk_nb(repo, "L")
    stats = repo.notebook_copy_stats(nb)
    assert stats["copyable"] is True          # 空库当然可拷贝
    assert set(stats["size"]) == {"bytes", "sources", "chunks", "nodes", "edges"}
```

（`_mk_nb` / `_rows` 已在 Task 1 建的测试文件顶部定义,直接复用。）

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_notebook_share_copy.py -k "share_sets or copy_stats" -v`
Expected: FAIL(方法不存在)

- [ ] **Step 3: 实现仓库方法**

在 `sqlite_repository.py` 顶部确保 `import secrets`(没有则加),然后加:

```python
    def share_notebook(self, notebook_id: str) -> dict:
        """开启分享(幂等):已分享则复用现有 token。返回 token + copyable + size。"""
        self.get_notebook(notebook_id)  # 不存在 → KeyError
        with self._write() as db:
            row = db.execute(
                "SELECT is_shared, share_token FROM notebooks WHERE id=?", (notebook_id,)).fetchone()
            token = row["share_token"] if (row["is_shared"] and row["share_token"]) \
                else f"shr-{secrets.token_urlsafe(16)}"
            db.execute("UPDATE notebooks SET is_shared=1, share_token=?, updated_at=? WHERE id=?",
                       (token, _now(), notebook_id))
        stats = self.notebook_copy_stats(notebook_id)
        return {"share_token": token, "copyable": stats["copyable"], "size": stats["size"]}

    def unshare_notebook(self, notebook_id: str) -> None:
        self.get_notebook(notebook_id)
        with self._write() as db:
            db.execute("UPDATE notebooks SET is_shared=0, share_token=NULL, updated_at=? WHERE id=?",
                       (_now(), notebook_id))

    def find_notebook_by_share_token(self, token: str) -> "str | None":
        if not token:
            return None
        with self._connect() as db:
            row = db.execute(
                "SELECT id FROM notebooks WHERE share_token=? AND is_shared=1", (token,)).fetchone()
        return row["id"] if row else None

    def notebook_copy_stats(self, notebook_id: str) -> dict:
        """便宜的大小盘点 + 是否在拷贝阈值内。"""
        with self._connect() as db:
            def one(sql):
                return db.execute(sql, (notebook_id,)).fetchone()[0]
            b = one("SELECT COALESCE(SUM(file_size),0) FROM sources WHERE notebook_id=?")
            src = one("SELECT COUNT(*) FROM sources WHERE notebook_id=?")
            ch = one("SELECT COUNT(*) FROM chunks WHERE notebook_id=?")
            nd = one("SELECT COUNT(*) FROM knowledge_objects WHERE notebook_id=?")
            eg = one("SELECT COUNT(*) FROM knowledge_relations WHERE notebook_id=?")
        copyable = (b <= self.settings.notebook_copy_max_bytes) \
            and ((ch + nd) <= self.settings.notebook_copy_max_rows)
        return {"copyable": copyable,
                "size": {"bytes": b, "sources": src, "chunks": ch, "nodes": nd, "edges": eg}}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_notebook_share_copy.py -k "share_sets or copy_stats" -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_notebook_share_copy.py
git commit -m "feat(share): share/unshare/find_by_token/copy_stats 仓库方法"
```

---

## Task 4: JSON id 重写纯函数 `_remap_json_ids`

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(纯函数,建议模块级或 `@staticmethod`)
- Test: `backend/tests/test_notebook_share_copy.py`

- [ ] **Step 1: 写失败测试**

```python
def test_remap_json_ids_scalars_and_arrays():
    from app.services.sqlite_repository import _remap_json_ids
    maps = {"element_id": {"el-1": "el-A"}, "element_ids": {"el-1": "el-A", "el-2": "el-B"},
            "source_id": {"src-1": "src-A"}, "object_id": {"ko-1": "ko-A"}}
    payload = {
        "source_id": "src-1",
        "steps": [{"element_id": "el-1", "quote": "keep me"}],
        "evidence": [{"element_id": "el-2", "source_id": "src-1", "quoted_span": "keep"}],
        "element_ids": ["el-1", "el-2", "el-unknown"],
        "note": "untouched",
    }
    out = _remap_json_ids(payload, maps)
    assert out["source_id"] == "src-A"
    assert out["steps"][0]["element_id"] == "el-A"
    assert out["steps"][0]["quote"] == "keep me"
    assert out["evidence"][0]["element_id"] == "el-B"
    assert out["evidence"][0]["source_id"] == "src-A"
    assert out["element_ids"] == ["el-A", "el-B", "el-unknown"]  # 未命中的原样
    assert out["note"] == "untouched"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_notebook_share_copy.py::test_remap_json_ids_scalars_and_arrays -v`
Expected: FAIL(`ImportError`)

- [ ] **Step 3: 实现纯函数**

在 `sqlite_repository.py` 模块级(靠近其它模块级 helper)加:

```python
def _remap_json_ids(value, maps: dict):
    """递归重写 JSON 里的 id 引用(拷贝 notebook 用)。按键名路由到对应映射:
    element_id/source_id/object_id → 标量替换;element_ids → 数组逐元素替换。
    映射里没有的值原样保留。maps 形如 {"element_id": {...}, "element_ids": {...}, ...}。"""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in ("element_id", "source_id", "object_id") and isinstance(v, str):
                out[k] = maps.get(k, {}).get(v, v)
            elif k == "element_ids" and isinstance(v, list):
                m = maps.get("element_ids", {})
                out[k] = [m.get(x, x) if isinstance(x, str) else _remap_json_ids(x, maps) for x in v]
            else:
                out[k] = _remap_json_ids(v, maps)
        return out
    if isinstance(value, list):
        return [_remap_json_ids(x, maps) for x in value]
    return value
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_notebook_share_copy.py::test_remap_json_ids_scalars_and_arrays -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_notebook_share_copy.py
git commit -m "feat(share): _remap_json_ids 纯函数(JSON 内嵌 id 重写)"
```

---

## Task 5: 拷贝引擎 `copy_notebook`(核心)

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(新方法 `copy_notebook` + 私有 `_insert_row`)
- Test: `backend/tests/test_notebook_share_copy.py`

- [ ] **Step 1: 写失败测试(用一个"种齐各表"的 helper 造真数据,断言拷贝正确)**

```python
def _seed_full_notebook(repo, owner="user-local"):
    """种一个各表都有数据、且含交叉引用的小 notebook,返回 nb_id。"""
    import json, uuid
    from app.services.sqlite_repository import _now
    now = _now()
    nb = f"nb-{uuid.uuid4().hex[:10]}"; s = f"src-{uuid.uuid4().hex[:6]}"
    e1 = f"el-{uuid.uuid4().hex[:6]}"; c1 = f"ck-{uuid.uuid4().hex[:6]}"
    o1 = f"ko-{uuid.uuid4().hex[:6]}"; o2 = f"ko-{uuid.uuid4().hex[:6]}"
    r1 = f"rel-{uuid.uuid4().hex[:6]}"
    with repo._write() as db:
        db.execute("INSERT INTO notebooks (id,name,purpose,primary_domain,status,created_by,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?)", (nb,"Orig","","Semiconductor","draft",owner,now,now))
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,file_size,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?)", (s,nb,"S","document","s.md","",10,now,now))
        db.execute("INSERT INTO source_elements (id,source_id,element_type,location_label,text,created_at) "
                   "VALUES (?,?,?,?,?,?)", (e1,s,"para","p1","hello",now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?)", (c1,nb,s,"chunk txt",json.dumps([e1]),now))
        db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                   (c1,nb,json.dumps([0.1,0.2]),now))
        for o in (o1,o2):
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,source_id,payload,evidence,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       (o,nb,"concept",s,json.dumps({"name":"x"}),json.dumps([{"element_id":e1,"source_id":s}]),now,now))
            db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                       (o,nb,json.dumps([0.3]),now))
        db.execute("INSERT INTO knowledge_relations (id,notebook_id,source_id,source_object_id,target_object_id,edge_type,evidence,created_at) "
                   "VALUES (?,?,?,?,?,?,?,?)", (r1,nb,s,o1,o2,"rel",json.dumps([{"element_id":e1}]),now))
        db.execute("INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,created_at) "
                   "VALUES (?,?,?,?,?,?)", (f"cl-{uuid.uuid4().hex[:6]}",nb,o1,o1,"x",now))
        # 一个不该被拷贝的对话
        db.execute("INSERT INTO conversations (id,notebook_id,title,created_by,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?)", (f"cv-{uuid.uuid4().hex[:6]}",nb,"chat",owner,now,now))
    return nb


# (_rows 已在 Task 1 测试文件顶部定义,复用)


def test_copy_notebook_deep_copies_and_remaps(repo):
    src = _seed_full_notebook(repo)
    new = repo.copy_notebook(src, new_owner_id="user-bob")
    assert new.id != src and new.tier == "personal"
    with repo._connect() as db:
        assert db.execute("SELECT created_by FROM notebooks WHERE id=?", (new.id,)).fetchone()[0] == "user-bob"
        assert db.execute("SELECT is_shared,share_token FROM notebooks WHERE id=?", (new.id,)).fetchone()[0] == 0
    # 行数一致
    for t in ("sources","chunks","knowledge_objects","knowledge_relations","concept_clusters"):
        assert len(_rows(repo, t, new.id)) == len(_rows(repo, t, src)), t
    # 关系指向副本内 objects(无悬空)
    with repo._connect() as db:
        obj_ids = {r["id"] for r in _rows(repo, "knowledge_objects", new.id)}
        rel = _rows(repo, "knowledge_relations", new.id)[0]
        assert rel["source_object_id"] in obj_ids and rel["target_object_id"] in obj_ids
        # chunk.element_ids 已重写到副本 element
        import json
        new_elem_ids = {r["id"] for r in db.execute(
            "SELECT se.id FROM source_elements se JOIN sources s ON s.id=se.source_id WHERE s.notebook_id=?", (new.id,))}
        ck = _rows(repo, "chunks", new.id)[0]
        assert json.loads(ck["element_ids"])[0] in new_elem_ids
        # evidence.element_id 已重写
        ev = json.loads(_rows(repo, "knowledge_objects", new.id)[0]["evidence"])
        assert ev[0]["element_id"] in new_elem_ids
    # conversations 不被拷贝
    with repo._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM conversations WHERE notebook_id=?", (new.id,)).fetchone()[0] == 0
    # 原库不受影响
    assert len(_rows(repo, "knowledge_objects", src)) == 2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_notebook_share_copy.py::test_copy_notebook_deep_copies_and_remaps -v`
Expected: FAIL(`copy_notebook` 不存在)

- [ ] **Step 3: 实现 `copy_notebook` + `_insert_row`**

确保 `sqlite_repository.py` 顶部有 `import shutil`(已有)、`import json`(已有)、`from pathlib import Path`(已有)。加方法:

```python
    @staticmethod
    def _insert_row(db, table: str, d: dict) -> None:
        cols = list(d.keys())
        db.execute(
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [d[c] for c in cols],
        )

    def copy_notebook(self, source_notebook_id: str, *, new_owner_id: str,
                      new_name: "str | None" = None) -> "NotebookSummary":
        """把 notebook 深拷贝成归 new_owner_id 的新库:全表 id 重映射(列 + JSON)+ 磁盘文件复制
        + 完整性自检,单事务、失败回滚 + 清理文件。不拷 conversations/answers/feedback/派生索引。
        调用方负责 size 门与 is_shared 校验。"""
        src = self.get_notebook(source_notebook_id)  # KeyError if missing
        new_id = f"nb-{uuid4().hex[:10]}"
        now = _now()
        name = new_name or f"{src.name} (副本)"

        def _nid(old: str) -> str:
            prefix = old.split("-", 1)[0] if old else "id"
            return f"{prefix}-{uuid4().hex[:10]}"

        src_dir = self.storage_dir / "notebooks" / source_notebook_id
        dst_dir = self.storage_dir / "notebooks" / new_id
        copied_files = False
        try:
            if src_dir.exists():
                shutil.copytree(src_dir, dst_dir)
                copied_files = True
            with self._write() as db:
                # 1) notebooks 行:动态复制全列,覆盖关键字段
                nb = dict(db.execute("SELECT * FROM notebooks WHERE id=?", (source_notebook_id,)).fetchone())
                nb.update(id=new_id, name=name, created_by=new_owner_id, tier="personal",
                          is_shared=0, share_token=None, created_at=now, updated_at=now)
                self._insert_row(db, "notebooks", nb)

                smap, emap, cmap, omap, rmap = {}, {}, {}, {}, {}

                # 2) sources(+ file_path 指向新目录)
                for r in db.execute("SELECT * FROM sources WHERE notebook_id=?", (source_notebook_id,)).fetchall():
                    d = dict(r); d["id"] = smap.setdefault(r["id"], _nid(r["id"])); d["notebook_id"] = new_id
                    if d.get("file_path"):
                        d["file_path"] = str(dst_dir / Path(d["file_path"]).name)
                    self._insert_row(db, "sources", d)

                # 3) source_elements(经 source_id 关联;无 notebook_id 列)
                for r in db.execute(
                    "SELECT se.* FROM source_elements se JOIN sources s ON s.id=se.source_id "
                    "WHERE s.notebook_id=?", (source_notebook_id,)).fetchall():
                    d = dict(r); d["id"] = emap.setdefault(r["id"], _nid(r["id"]))
                    d["source_id"] = smap[r["source_id"]]
                    self._insert_row(db, "source_elements", d)

                jmaps = {"element_id": emap, "element_ids": emap, "source_id": smap, "object_id": omap}

                # 4) chunks(element_ids 是 JSON 数组)
                for r in db.execute("SELECT * FROM chunks WHERE notebook_id=?", (source_notebook_id,)).fetchall():
                    d = dict(r); d["id"] = cmap.setdefault(r["id"], _nid(r["id"])); d["notebook_id"] = new_id
                    d["source_id"] = smap[r["source_id"]]
                    d["element_ids"] = json.dumps(_remap_json_ids(json.loads(d.get("element_ids") or "[]"), jmaps))
                    self._insert_row(db, "chunks", d)

                # 5) knowledge_objects(source_id / source_candidate_id + evidence/payload JSON)
                for r in db.execute("SELECT * FROM knowledge_objects WHERE notebook_id=?", (source_notebook_id,)).fetchall():
                    d = dict(r); d["id"] = omap.setdefault(r["id"], _nid(r["id"])); d["notebook_id"] = new_id
                    d["source_id"] = smap.get(r["source_id"], r["source_id"])
                    if d.get("source_candidate_id"):
                        d["source_candidate_id"] = smap.get(r["source_candidate_id"], r["source_candidate_id"])
                    d["payload"] = json.dumps(_remap_json_ids(json.loads(d.get("payload") or "{}"), jmaps))
                    d["evidence"] = json.dumps(_remap_json_ids(json.loads(d.get("evidence") or "[]"), jmaps))
                    self._insert_row(db, "knowledge_objects", d)

                # 6) knowledge_relations(两端 object + source + evidence JSON)
                for r in db.execute("SELECT * FROM knowledge_relations WHERE notebook_id=?", (source_notebook_id,)).fetchall():
                    d = dict(r); d["id"] = rmap.setdefault(r["id"], _nid(r["id"])); d["notebook_id"] = new_id
                    d["source_id"] = smap.get(r["source_id"], r["source_id"])
                    d["source_object_id"] = omap[r["source_object_id"]]
                    d["target_object_id"] = omap[r["target_object_id"]]
                    d["evidence"] = json.dumps(_remap_json_ids(json.loads(d.get("evidence") or "[]"), jmaps))
                    self._insert_row(db, "knowledge_relations", d)

                # 7) embeddings(主键=外键,按映射改)
                for r in db.execute("SELECT * FROM chunk_embeddings WHERE notebook_id=?", (source_notebook_id,)).fetchall():
                    d = dict(r); d["chunk_id"] = cmap[r["chunk_id"]]; d["notebook_id"] = new_id
                    self._insert_row(db, "chunk_embeddings", d)
                for r in db.execute(
                    "SELECT ee.* FROM element_embeddings ee JOIN sources s ON s.id=ee.source_id "
                    "WHERE s.notebook_id=?", (source_notebook_id,)).fetchall():
                    d = dict(r); d["element_id"] = emap[r["element_id"]]; d["source_id"] = smap[r["source_id"]]; d["notebook_id"] = new_id
                    self._insert_row(db, "element_embeddings", d)
                for r in db.execute("SELECT * FROM knowledge_embeddings WHERE notebook_id=?", (source_notebook_id,)).fetchall():
                    d = dict(r); d["object_id"] = omap[r["object_id"]]; d["notebook_id"] = new_id
                    self._insert_row(db, "knowledge_embeddings", d)
                for r in db.execute("SELECT * FROM relation_embeddings WHERE notebook_id=?", (source_notebook_id,)).fetchall():
                    d = dict(r); d["relation_id"] = rmap[r["relation_id"]]; d["notebook_id"] = new_id
                    self._insert_row(db, "relation_embeddings", d)

                # 8) concept_clusters(canonical_id / member_object_id → object 映射)
                for r in db.execute("SELECT * FROM concept_clusters WHERE notebook_id=?", (source_notebook_id,)).fetchall():
                    d = dict(r); d["id"] = _nid(r["id"]); d["notebook_id"] = new_id
                    d["canonical_id"] = omap.get(r["canonical_id"], r["canonical_id"])
                    d["member_object_id"] = omap.get(r["member_object_id"], r["member_object_id"])
                    self._insert_row(db, "concept_clusters", d)

                # 9) 自定义 object_schemas(notebook_id 命中的才拷)
                for r in db.execute("SELECT * FROM object_schemas WHERE notebook_id=?", (source_notebook_id,)).fetchall():
                    d = dict(r); d["notebook_id"] = new_id
                    self._insert_row(db, "object_schemas", d)

                # 10) 完整性自检:行数一致 + 关系无悬空
                for t in ("sources", "chunks", "knowledge_objects", "knowledge_relations", "concept_clusters"):
                    a = db.execute(f"SELECT COUNT(*) FROM {t} WHERE notebook_id=?", (new_id,)).fetchone()[0]
                    b = db.execute(f"SELECT COUNT(*) FROM {t} WHERE notebook_id=?", (source_notebook_id,)).fetchone()[0]
                    if a != b:
                        raise RuntimeError(f"copy_notebook: {t} 行数不一致 {a}!={b}")
                dangling = db.execute(
                    "SELECT COUNT(*) FROM knowledge_relations r WHERE r.notebook_id=? AND ("
                    "r.source_object_id NOT IN (SELECT id FROM knowledge_objects WHERE notebook_id=?) OR "
                    "r.target_object_id NOT IN (SELECT id FROM knowledge_objects WHERE notebook_id=?))",
                    (new_id, new_id, new_id)).fetchone()[0]
                if dangling:
                    raise RuntimeError("copy_notebook: 关系存在悬空引用")
            return self.get_notebook(new_id)
        except Exception:
            if copied_files:
                shutil.rmtree(dst_dir, ignore_errors=True)
            raise
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_notebook_share_copy.py::test_copy_notebook_deep_copies_and_remaps -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_notebook_share_copy.py
git commit -m "feat(share): copy_notebook 深拷贝引擎(全表 id 重映射+文件+完整性自检)"
```

---

## Task 6: 响应 schema + 4 个路由

**Files:**
- Modify: `backend/app/models/schemas.py`(加 `ShareResponse`、`SharedPreview`)
- Modify: `backend/app/services/sqlite_repository.py`(加 `shared_preview`)
- Modify: `backend/app/api/routes.py`(加 4 路由 + import)
- Test: `backend/tests/test_notebook_share_copy.py`(用 FastAPI `TestClient`)

- [ ] **Step 1: 写失败测试**

`repo` 种数据、`client` 驱动 API,共享同一 tmp DB(见 Task 1 脚手架):

```python
def test_share_preview_copy_end_to_end(repo, client):
    src = _seed_full_notebook(repo, owner="user-local")  # 归 seeded admin(=API 调用者)
    # 分享
    r = client.post(f"/api/notebooks/{src}/share"); assert r.status_code == 200
    token = r.json()["share_token"]; assert r.json()["copyable"] is True
    # 预览
    p = client.get(f"/api/shared/{token}"); assert p.status_code == 200
    assert p.json()["mode"] == "copy" and p.json()["source_count"] == 1
    # 拷贝
    c = client.post(f"/api/shared/{token}/copy"); assert c.status_code == 200
    new_id = c.json()["id"]; assert new_id != src
    assert len(_rows(repo, "knowledge_objects", new_id)) == 2
    # 取消分享 → 预览/拷贝 404
    assert client.delete(f"/api/notebooks/{src}/share").status_code == 204
    assert client.get(f"/api/shared/{token}").status_code == 404
    assert client.post(f"/api/shared/{token}/copy").status_code == 404


def test_copy_refuses_too_large(repo, client, monkeypatch):
    # 首个 client 请求才触发 repository() 重建(conftest 已清缓存),此时读到 =1
    monkeypatch.setenv("NOTEBOOK_COPY_MAX_ROWS", "1")
    src = _seed_full_notebook(repo, owner="user-local")
    token = client.post(f"/api/notebooks/{src}/share").json()["share_token"]
    assert client.get(f"/api/shared/{token}").json()["mode"] == "too_large"
    assert client.post(f"/api/shared/{token}/copy").status_code == 409
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_notebook_share_copy.py -k "end_to_end or too_large" -v`
Expected: FAIL(路由不存在 → 404/405)

- [ ] **Step 3a: 加响应 schema(`app/models/schemas.py`)**

```python
class ShareResponse(BaseModel):
    share_token: str
    copyable: bool
    size: Dict[str, int]


class SharedPreview(BaseModel):
    name: str
    owner_display: str
    source_count: int
    node_count: int
    edge_count: int
    source_titles: List[str]
    mode: str
    size: Dict[str, int]
```

- [ ] **Step 3b: 加 `shared_preview` 仓库方法(`sqlite_repository.py`)**

```python
    def shared_preview(self, notebook_id: str) -> dict:
        nb = self.get_notebook(notebook_id)
        stats = self.notebook_copy_stats(notebook_id)
        with self._connect() as db:
            owner = db.execute(
                "SELECT u.username FROM notebooks nb LEFT JOIN users u ON u.id=nb.created_by "
                "WHERE nb.id=?", (notebook_id,)).fetchone()
            titles = [r["title"] for r in db.execute(
                "SELECT title FROM sources WHERE notebook_id=? ORDER BY created_at LIMIT 50",
                (notebook_id,)).fetchall()]
        return {
            "name": nb.name,
            "owner_display": (owner["username"] if owner and owner["username"] else ""),
            "source_count": stats["size"]["sources"],
            "node_count": stats["size"]["nodes"],
            "edge_count": stats["size"]["edges"],
            "source_titles": titles,
            "mode": "copy" if stats["copyable"] else "too_large",
            "size": stats["size"],
        }
```

- [ ] **Step 3c: 加 4 路由(`app/api/routes.py`)**

在 import 段补:`from app.models.schemas import ShareResponse, SharedPreview, UserProfile`(与现有 schema import 合并),并确保 `get_current_user` 已从 `app.api.deps` 导入(第 12 行已有)。加路由:

```python
@router.post("/notebooks/{notebook_id}/share", response_model=ShareResponse,
             dependencies=[Depends(require_notebook_access)])
def share_notebook_route(notebook_id: str) -> ShareResponse:
    try:
        return ShareResponse(**repository().share_notebook(notebook_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.delete("/notebooks/{notebook_id}/share", status_code=204,
               dependencies=[Depends(require_notebook_access)])
def unshare_notebook_route(notebook_id: str) -> None:
    try:
        repository().unshare_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.get("/shared/{token}", response_model=SharedPreview)
def shared_preview_route(token: str, user: UserProfile = Depends(get_current_user)) -> SharedPreview:
    nb_id = repository().find_notebook_by_share_token(token)
    if nb_id is None:
        raise HTTPException(status_code=404, detail="Shared notebook not found")
    return SharedPreview(**repository().shared_preview(nb_id))


@router.post("/shared/{token}/copy", response_model=NotebookSummary)
def copy_shared_route(token: str, user: UserProfile = Depends(get_current_user)) -> NotebookSummary:
    repo = repository()
    nb_id = repo.find_notebook_by_share_token(token)
    if nb_id is None:
        raise HTTPException(status_code=404, detail="Shared notebook not found")
    if not repo.notebook_copy_stats(nb_id)["copyable"]:
        raise HTTPException(status_code=409, detail="notebook too large to copy")
    return repo.copy_notebook(nb_id, new_owner_id=user.id)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_notebook_share_copy.py -k "end_to_end or too_large" -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/models/schemas.py backend/app/services/sqlite_repository.py backend/app/api/routes.py backend/tests/test_notebook_share_copy.py
git commit -m "feat(share): 4 端点(share/unshare/preview/copy)+ 响应 schema"
```

---

## Task 7: owner 隔离回归 + 全量

**Files:**
- Test: `backend/tests/test_notebook_share_copy.py`

- [ ] **Step 1: 写守卫测试**

```python
def test_non_owner_cannot_share(repo, client):
    # 造一个属于别人的库;当前用户(seeded admin=user-local)不是 owner
    other = _seed_full_notebook(repo, owner="user-someone-else")
    assert client.post(f"/api/notebooks/{other}/share").status_code == 404  # 不泄露存在性


def test_copy_appears_in_copier_list_and_original_untouched(repo, client):
    src = _seed_full_notebook(repo, owner="user-local")
    token = client.post(f"/api/notebooks/{src}/share").json()["share_token"]
    new_id = client.post(f"/api/shared/{token}/copy").json()["id"]
    ids = {n["id"] for n in client.get("/api/notebooks").json()}
    assert new_id in ids and src in ids  # copier==admin 两个都在
    # 原库对象数不变
    assert len(_rows(repo, "knowledge_objects", src)) == 2
```

- [ ] **Step 2: 跑测试确认失败/通过**

Run: `python -m pytest tests/test_notebook_share_copy.py -k "non_owner or copier_list" -v`
Expected: PASS(守卫复用现有 `require_notebook_access`,应直接过;若 `non_owner` 因 auth_optional 回退 admin 而误过,改为构造 admin 拥有的 client 后断言对 `user-someone-else` 库 403/404——见下方说明)

> `require_notebook_access` 对非 owner 返回 404。测试里当前用户是 seeded admin(`user-local`),`other` 库 owner 是 `user-someone-else`,故 admin 也不是 owner → 404。(注:admin **无**跨 owner 越权,见 `user_can_access_notebook` 注释。)

- [ ] **Step 3: 全量回归**

Run: `python -m pytest -q`
Expected: 全绿(基线 1271 passed 之上只增不减、0 fail)。若某历史测试因新列/新路由变动,就地修正。

- [ ] **Step 4: 提交**

```bash
git add backend/tests/test_notebook_share_copy.py
git commit -m "test(share): owner 隔离守卫 + 拷贝独立性回归"
```

---

## 前端(后续独立小计划)

Phase 1 **后端 API 完成并验证后**,再出一个前端小计划(需先读 `frontend/app/page.tsx` 现有模式):notebook 内「分享」按钮(调 `POST/DELETE .../share`,展示分享码)+ 打开分享码的预览弹窗(`GET /shared/{token}` → `mode==copy` 显示「拷贝到我的空间」调 `POST .../copy` 后跳转;`too_large` 显示提示)。遵循 [[ui-polish-bar]]。本计划不含前端,保持后端可独立交付 + 可测。

## 收尾(全部 Task 完成后)

- 全量 `python -m pytest -q` 绿。
- 按 [[dev-flow-finish-with-pr]]:分支 `feat/notebook-share-copy` 已基于 origin/master;push → `gh pr create --base master`。PR 描述讲清「Phase 1:分享码+小库拷贝;大库只读共享=Phase 2」。
