# Notebook 分享 Phase 2(大库只读共享)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 大库只读共享——他人凭分享链接**加入**为只读成员,能浏览/问答但不能改库;owner 有「已分享总览」。

**Architecture:** 成员表 `notebook_members` + 把 `require_notebook_access` 拆为 `require_notebook_read`(owner∪成员)/`require_notebook_write`(仅 owner,= 现逻辑),**只把明确的读路由改挂 read、其余留 owner-only(默认最严兜底)**;子资源改 member-aware(source 读/conversation 按 creator);`list_notebooks` 合并自有∪加入;join/leave/撤销踢全员 + `GET /notebooks/shared-by-me` 总览。

**Tech Stack:** FastAPI 同步路由、`SQLiteRepository`、pytest;前端 Next.js 单页 `page.tsx` + `app/notebook-share.ts` + `node --test`。

**Spec:** `docs/superpowers/specs/2026-07-01-notebook-share-phase2-readonly-design.md`

## Global Constraints
- 分支 `feat/notebook-share-copy`(续 Phase 1,别新建分支)。别 push(我来提 PR)。
- 测试解释器:从 `backend/` 跑 `/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest`。全程保持全量绿(基线 1304 passed / 1 skipped,只增不减)。
- **安全铁律**:①`user_can_access_notebook`(owner-only,`sqlite_repository.py:1424`)**一字不改**——`require_notebook_write` 继续用它;加读权**只加新函数** `user_can_read_notebook`。②只把 spec §3.2 列出的读路由改挂 `require_notebook_read`,其余 `/notebooks/{id}` 路由**保持** `require_notebook_write`。
- 后端测试放 `backend/tests/test_notebook_share_readonly.py`(新建);前端测试进 `frontend/app/notebook-share.test.mjs`(已存在)。
- 弯引号有意([[frontend-curly-quotes-intentional]]);前端 UI 遵循 [[ui-polish-bar]]。

---

## Task 1: 成员表 + 成员/读权仓库方法

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(schema 迁移段 + 新方法)
- Test: `backend/tests/test_notebook_share_readonly.py`

**Interfaces:**
- Produces: `add_member(nb, user_id)`、`remove_member(nb, user_id)`、`kick_all_members(nb)`、`is_member(nb, user_id)->bool`、`list_members(nb)->list[dict]`(`{username, added_at}`)、`user_can_read_notebook(nb, user_id)->bool`(owner∪成员)、`user_can_read_source(source_id, user_id)->bool`。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_notebook_share_readonly.py
import uuid
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, _now


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _mk_user(repo, uid, username=None):
    with repo._write() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id,username,password_hash,role,created_at) VALUES (?,?,?,?,?)",
            (uid, username or uid, "x", "user", _now()))


def _mk_nb(repo, owner="user-local", name="NB"):
    nb = f"nb-{uuid.uuid4().hex[:10]}"
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebooks (id,name,purpose,primary_domain,status,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)", (nb, name, "", "Semiconductor", "draft", owner, _now(), _now()))
    return nb


def test_membership_crud_and_read_access(repo):
    nb = _mk_nb(repo, owner="user-local")
    _mk_user(repo, "user-bob", "b00000001")
    assert repo.is_member(nb, "user-bob") is False
    assert repo.user_can_read_notebook(nb, "user-bob") is False   # 非成员非 owner
    assert repo.user_can_read_notebook(nb, "user-local") is True  # owner 恒可读
    repo.add_member(nb, "user-bob")
    assert repo.is_member(nb, "user-bob") is True
    assert repo.user_can_read_notebook(nb, "user-bob") is True    # 成员可读
    assert [m["username"] for m in repo.list_members(nb)] == ["b00000001"]
    repo.add_member(nb, "user-bob")  # 幂等
    assert len(repo.list_members(nb)) == 1
    repo.kick_all_members(nb)
    assert repo.list_members(nb) == []
    assert repo.user_can_read_notebook(nb, "user-bob") is False


def test_user_can_read_source_follows_membership(repo):
    nb = _mk_nb(repo, owner="user-local")
    _mk_user(repo, "user-bob")
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,file_size,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?)", ("src-1", nb, "S", "document", "s.md", "", 0, _now(), _now()))
    assert repo.user_can_read_source("src-1", "user-bob") is False
    repo.add_member(nb, "user-bob")
    assert repo.user_can_read_source("src-1", "user-bob") is True
    assert repo.user_can_read_source("src-1", "user-local") is True  # owner
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_notebook_share_readonly.py -k "membership_crud or read_source" -v`
Expected: FAIL(表/方法不存在)

- [ ] **Step 3: 加迁移 + 方法**

在 notebooks 列迁移段(Phase 1 的 `is_shared`/`share_token` 迁移旁)加建表:

```python
            db.execute(
                "CREATE TABLE IF NOT EXISTS notebook_members ("
                "  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,"
                "  user_id TEXT NOT NULL REFERENCES users(id),"
                "  role TEXT NOT NULL DEFAULT 'reader',"
                "  added_at TEXT NOT NULL,"
                "  PRIMARY KEY (notebook_id, user_id))"
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_notebook_members_user ON notebook_members(user_id)")
```

在 `user_can_access_notebook`(:1424,**不改它**)之后加:

```python
    def is_member(self, notebook_id: str, user_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM notebook_members WHERE notebook_id=? AND user_id=?",
                (notebook_id, user_id)).fetchone()
        return row is not None

    def user_can_read_notebook(self, notebook_id: str, user_id: str) -> bool:
        """读权 = owner ∪ 成员。写权仍用 user_can_access_notebook(owner-only,不动)。"""
        return self.user_can_access_notebook(notebook_id, user_id) or self.is_member(notebook_id, user_id)

    def user_can_read_source(self, source_id: str, user_id: str) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT notebook_id FROM sources WHERE id=?", (source_id,)).fetchone()
        return bool(row) and self.user_can_read_notebook(row["notebook_id"], user_id)

    def add_member(self, notebook_id: str, user_id: str) -> None:
        with self._write() as db:
            db.execute(
                "INSERT OR IGNORE INTO notebook_members (notebook_id,user_id,role,added_at) "
                "VALUES (?,?,'reader',?)", (notebook_id, user_id, _now()))

    def remove_member(self, notebook_id: str, user_id: str) -> None:
        with self._write() as db:
            db.execute("DELETE FROM notebook_members WHERE notebook_id=? AND user_id=?",
                       (notebook_id, user_id))

    def kick_all_members(self, notebook_id: str) -> None:
        with self._write() as db:
            db.execute("DELETE FROM notebook_members WHERE notebook_id=?", (notebook_id,))

    def list_members(self, notebook_id: str) -> list:
        with self._connect() as db:
            rows = db.execute(
                "SELECT u.username AS username, m.added_at AS added_at FROM notebook_members m "
                "JOIN users u ON u.id=m.user_id WHERE m.notebook_id=? ORDER BY m.added_at ASC",
                (notebook_id,)).fetchall()
        return [{"username": r["username"], "added_at": r["added_at"]} for r in rows]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_notebook_share_readonly.py -k "membership_crud or read_source" -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_notebook_share_readonly.py
git commit -m "feat(share-p2): notebook_members 表 + 成员/读权仓库方法"
```

---

## Task 2: unshare 踢全员 + 预览 mode=readonly

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`unshare_notebook`:1238、`shared_preview`:1267、`notebook_copy_stats`:1252)
- Test: `backend/tests/test_notebook_share_readonly.py`

**Interfaces:**
- Consumes: `add_member`/`list_members`/`is_member`(Task 1)、`notebook_copy_stats`(Phase 1)。
- Produces: `shared_preview(nb)["mode"]` ∈ `{"copy","readonly"}`(大库=readonly);`unshare_notebook` 清成员。

- [ ] **Step 1: 写失败测试**

```python
def test_unshare_kicks_members(repo):
    nb = _mk_nb(repo, owner="user-local")
    _mk_user(repo, "user-bob")
    repo.share_notebook(nb)
    repo.add_member(nb, "user-bob")
    repo.unshare_notebook(nb)
    assert repo.list_members(nb) == []


def test_preview_mode_readonly_for_large(repo, monkeypatch):
    nb = _mk_nb(repo, owner="user-local")
    with repo._write() as db:  # 造 2 个 knowledge_objects 触发超阈
        for i in range(2):
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,created_at,updated_at) "
                       "VALUES (?,?,?,?,?)", (f"ko-{i}", nb, "concept", _now(), _now()))
    repo.settings.notebook_copy_max_rows = 1  # 逼超阈
    assert repo.shared_preview(nb)["mode"] == "readonly"
    repo.settings.notebook_copy_max_rows = 5000
    assert repo.shared_preview(nb)["mode"] == "copy"
```

- [ ] **Step 2: 跑确认失败**

Run: `python -m pytest tests/test_notebook_share_readonly.py -k "unshare_kicks or mode_readonly" -v`
Expected: FAIL(`mode` 现值为 `too_large`;unshare 不清成员)

- [ ] **Step 3: 改三处**

`unshare_notebook`(:1238)在 UPDATE 后加清成员:

```python
    def unshare_notebook(self, notebook_id: str) -> None:
        self.get_notebook(notebook_id)
        with self._write() as db:
            db.execute("UPDATE notebooks SET is_shared=0, share_token=NULL, updated_at=? WHERE id=?",
                       (_now(), notebook_id))
            db.execute("DELETE FROM notebook_members WHERE notebook_id=?", (notebook_id,))
```

`shared_preview`(:1267)里 `mode` 从 `"too_large"` 改 `"readonly"`——定位现有 `"mode": "copy" if stats["copyable"] else "too_large"`,改为:

```python
            "mode": "copy" if stats["copyable"] else "readonly",
```

（`notebook_copy_stats` 不改;mode 仅在 preview/ shared_by_me 里由 copyable 派生。）

- [ ] **Step 4: 跑确认通过**

Run: `python -m pytest tests/test_notebook_share_readonly.py -k "unshare_kicks or mode_readonly" -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_notebook_share_readonly.py
git commit -m "feat(share-p2): unshare 踢全员 + 大库预览 mode=readonly"
```

---

## Task 3: 拆守卫 + 读路由改挂(deps.py + routes.py)

**Files:**
- Modify: `backend/app/api/deps.py`(:52-58)
- Modify: `backend/app/api/routes.py`(spec §3.2 列出的读路由改挂)
- Test: `backend/tests/test_notebook_share_readonly.py`

**Interfaces:**
- Consumes: `user_can_read_notebook`(Task 1)、现有 `require_notebook_access`。
- Produces: `require_notebook_read`(owner∪成员)、`require_notebook_write`(= 现 `require_notebook_access`);`deps` 同时导出三个名字。

- [ ] **Step 1: 写失败测试(守卫矩阵——成员读放行、写被拒)**

```python
from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app
    return TestClient(app)


def _login(client, username, password="pw123456"):
    client.post("/api/auth/register", json={"username": username, "password": password})
    tok = client.post("/api/auth/login", json={"username": username, "password": password}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}


# 读路由(成员应 200)与写路由(成员应 404)的枚举样本。完整清单见 spec §3.2。
READ_ROUTES = ["", "/analytics", "/sources", "/graph", "/search?q=x", "/conversations"]
WRITE_ROUTES = [("patch", ""), ("delete", ""), ("post", "/kg/rebuild"), ("post", "/tier"), ("post", "/share")]


def test_member_can_read_cannot_write(tmp_path, monkeypatch, repo):
    # repo fixture 与 client 共用同一 tmp DB(同 tmp_path)
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000001")
    nb = client.post("/api/notebooks", json={"name": "L"}, headers=owner_h).json()["id"]
    bob_h = _login(client, "b00000002")
    bob_id = client.get("/api/me", headers=bob_h).json()["id"]
    repo.add_member(nb, bob_id)   # bob 成为只读成员
    for suffix in READ_ROUTES:
        r = client.get(f"/api/notebooks/{nb}{suffix}", headers=bob_h)
        assert r.status_code == 200, (suffix, r.status_code)
    for method, suffix in WRITE_ROUTES:
        r = client.request(method.upper(), f"/api/notebooks/{nb}{suffix}", headers=bob_h,
                           json={} if method in ("post", "patch") else None)
        assert r.status_code == 404, (method, suffix, r.status_code)  # 非 owner→404 不泄露
```

> 注:此测试同时用 `repo`(种成员)与 `client`(HTTP);两者靠同 `tmp_path` 的 `DATABASE_URL` 指同一 DB(见 Phase 1 计划同款)。`repo` fixture 已在 Task 1 定义;`client` 需要真实登录(不设 `AUTH_OPTIONAL`,用注册/登录拿 token),因为要区分 owner/bob 两个身份。

- [ ] **Step 2: 跑确认失败**

Run: `python -m pytest tests/test_notebook_share_readonly.py::test_member_can_read_cannot_write -v`
Expected: FAIL(读路由现对成员 404——因为还没拆守卫)

- [ ] **Step 3: 拆守卫 + 改挂**

`deps.py`:把现有 `require_notebook_access` 重命名语义为 write,并加 read。改 `require_notebook_access` 那段(:52-58)为:

```python
async def require_notebook_write(
    notebook_id: str, user: UserProfile = Depends(get_current_user)
) -> str:
    """写守卫:仅 owner。非 owner → 404(不泄露存在性)。"""
    if not repository().user_can_access_notebook(notebook_id, user.id):
        raise HTTPException(status_code=404, detail="Notebook not found")
    return notebook_id


async def require_notebook_read(
    notebook_id: str, user: UserProfile = Depends(get_current_user)
) -> str:
    """读守卫:owner ∪ 只读成员。"""
    if not repository().user_can_read_notebook(notebook_id, user.id):
        raise HTTPException(status_code=404, detail="Notebook not found")
    return notebook_id


# 向后兼容别名:老代码/未分类路由默认仍是 owner-only(默认最严)。
require_notebook_access = require_notebook_write
```

`routes.py`:把 import 补上 `require_notebook_read`、`require_notebook_write`(与现有 `require_notebook_access` 同处 import)。然后把 spec §3.2 **读清单**里的路由 `dependencies=[Depends(require_notebook_access)]` 改成 `[Depends(require_notebook_read)]`。逐个(共 ~15 处):
`GET /notebooks/{id}`、`/analytics`、`/sources`、`/knowledge-types`、`/knowledge`、`/duplicates`、`/graph`、`/search`、`POST /ask`、`POST /ask/stream`、`GET /conversations`、`GET /unified-kg`、`/unified-kg/status`、`/unified-kg/pending-merges`、`/edge-review-queue`、`GET /concepts/{cid}/detail`、`/objects/{oid}/context`、`/objects/{oid}/neighbors`、`/kg/conflicts/pending`。
**其余 `/notebooks/{id}` 写路由不动**(仍 `require_notebook_access` = write)。

> 逐个改挂时用精确定位:每条 `@router.<m>("<path>", ..., dependencies=[Depends(require_notebook_access)])` 只把该行的 `require_notebook_access` 换成 `require_notebook_read`。别全局替换(会误伤写路由)。

- [ ] **Step 4: 跑确认通过 + 全量**

Run: `python -m pytest tests/test_notebook_share_readonly.py::test_member_can_read_cannot_write -v && python -m pytest -q`
Expected: PASS;全量绿(0 fail)。

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/deps.py backend/app/api/routes.py backend/tests/test_notebook_share_readonly.py
git commit -m "feat(share-p2): 拆 require_notebook_read/write + 读路由改挂(默认最严)"
```

---

## Task 4: 子资源 member-aware(source 读 / conversation 按 creator)

**Files:**
- Modify: `backend/app/api/routes.py`(source :339-366、conversation :652-681、answer feedback)
- Modify: `backend/app/services/sqlite_repository.py`(`conversation_owner`:1440)
- Test: `backend/tests/test_notebook_share_readonly.py`

**Interfaces:**
- Consumes: `user_can_read_source`(Task 1)。
- Produces: `conversation_owner` 返回**对话创建者 `created_by`**(非 notebook owner)。

- [ ] **Step 1: 写失败测试**

```python
def test_member_reads_source_but_cannot_delete(tmp_path, monkeypatch, repo):
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000003")
    nb = client.post("/api/notebooks", json={"name": "L"}, headers=owner_h).json()["id"]
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,file_size,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?)", ("src-x", nb, "S", "document", "s.md", "", 0, _now(), _now()))
    bob_h = _login(client, "b00000004")
    bob_id = client.get("/api/me", headers=bob_h).json()["id"]
    repo.add_member(nb, bob_id)
    assert client.get("/api/sources/src-x", headers=bob_h).status_code == 200      # 成员可读
    assert client.delete("/api/sources/src-x", headers=bob_h).status_code == 404   # 成员不能删


def test_conversation_owner_is_creator_not_notebook_owner(repo):
    nb = _mk_nb(repo, owner="user-owner")
    _mk_user(repo, "user-owner"); _mk_user(repo, "user-mbr")
    with repo._write() as db:
        db.execute("INSERT INTO conversations (id,notebook_id,title,created_by,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?)", ("cv-1", nb, "chat", "user-mbr", _now(), _now()))
    assert repo.conversation_owner("cv-1") == "user-mbr"   # 创建者,不是 notebook owner
```

- [ ] **Step 2: 跑确认失败**

Run: `python -m pytest tests/test_notebook_share_readonly.py -k "reads_source or conversation_owner_is_creator" -v`
Expected: FAIL

- [ ] **Step 3: 改**

`conversation_owner`(sqlite_repository.py:1440)改为返回对话创建者:

```python
    def conversation_owner(self, conversation_id: str) -> "str | None":
        with self._connect() as db:
            row = db.execute(
                "SELECT created_by AS owner FROM conversations WHERE id = ?",
                (conversation_id,)).fetchone()
        return row["owner"] if row else None
```

`routes.py` 的两个 source **读**路由(`get_source`:340、`source_elements`:360)把 `source_owner(source_id) != user.id` 改成读权判定:

```python
    if not repository().user_can_read_source(source_id, user.id):
        raise HTTPException(status_code=404, detail="Source not found")
```

（`parse_source`:350、`delete_source`:370 两个**写**路由**不改**,仍 `source_owner != user.id`。）
`answers/{id}/feedback` 路由:把其 owner 校验(若为 `answer_owner(...)!=user.id`)改为放行「父 notebook 可读」——用 `answer_owner` 拿 notebook 无直接接口,故最小改动:保留现状(owner-only)**除非**现有测试要求成员可反馈;Phase 2 spec 允许成员反馈,但为不扩面,**本任务仅改 source 读 + conversation_owner**,feedback 留 owner-only(记入 spec 偏差,不阻断)。

> conversation 单用户不变性:自有 notebook 里对话创建者==owner,故 `get/rename/delete conversation` 行为对 owner 不变;共享库里成员管自己的、`conversation_owner` 按 creator 天然隔离。

- [ ] **Step 4: 跑确认通过 + 全量**

Run: `python -m pytest tests/test_notebook_share_readonly.py -k "reads_source or conversation_owner" -v && python -m pytest -q`
Expected: PASS;全量绿(尤其 `test_notebook_share_copy.py`、既有 conversation 测试不回归)。

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/routes.py backend/app/services/sqlite_repository.py backend/tests/test_notebook_share_readonly.py
git commit -m "feat(share-p2): source 读 member-aware + conversation 按 creator 判权"
```

---

## Task 5: list_notebooks 合并 + NotebookSummary.access/shared_from

**Files:**
- Modify: `backend/app/models/schemas.py`(`NotebookSummary`)
- Modify: `backend/app/services/sqlite_repository.py`(`list_notebooks`:1174)
- Test: `backend/tests/test_notebook_share_readonly.py`

**Interfaces:**
- Produces: `NotebookSummary.access: "owner"|"reader"`、`.shared_from: str`;`list_notebooks` 返回自有∪加入。

- [ ] **Step 1: 写失败测试**

```python
def test_list_notebooks_includes_joined_marked_reader(repo):
    owner_nb = _mk_nb(repo, owner="user-local", name="Mine")
    other_nb = _mk_nb(repo, owner="user-alice", name="Alice's")
    _mk_user(repo, "user-alice", "a00000009")
    repo.add_member(other_nb, "user-local")   # 当前用户(seeded admin=user-local)加入了 alice 的库
    got = {n.id: n for n in repo.list_notebooks()}
    assert got[owner_nb].access == "owner" and got[owner_nb].shared_from == ""
    assert got[other_nb].access == "reader" and got[other_nb].shared_from == "a00000009"
```

- [ ] **Step 2: 跑确认失败**

Run: `python -m pytest tests/test_notebook_share_readonly.py::test_list_notebooks_includes_joined_marked_reader -v`
Expected: FAIL(`access` 字段不存在 / 不含 joined)

- [ ] **Step 3: 改 schema + list_notebooks**

`schemas.py` 的 `NotebookSummary` 加两字段(默认 owner/空,向后兼容):

```python
    access: str = "owner"       # "owner" | "reader"(只读共享而来)
    shared_from: str = ""       # reader 时 = 原 owner 用户名
```

`list_notebooks`(:1174)改为自有∪加入,并标注 access/shared_from:

```python
    def list_notebooks(self) -> List[NotebookSummary]:
        uid = self.current_user().id
        out: List[NotebookSummary] = []
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM notebooks WHERE created_by=? ORDER BY created_at ASC", (uid,)).fetchall()
            for row in rows:
                nb = self._notebook_from_row(db, row)
                nb.access = "owner"
                out.append(nb)
            joined = db.execute(
                "SELECT nb.*, u.username AS _owner_username FROM notebook_members m "
                "JOIN notebooks nb ON nb.id=m.notebook_id "
                "LEFT JOIN users u ON u.id=nb.created_by "
                "WHERE m.user_id=? ORDER BY m.added_at ASC", (uid,)).fetchall()
            for row in joined:
                nb = self._notebook_from_row(db, row)
                nb.access = "reader"
                nb.shared_from = row["_owner_username"] or ""
                out.append(nb)
        return out
```

> `_notebook_from_row` 用 `row.keys()` 容错额外列(`_owner_username`),不会因多一列报错(已核实其只读命名列)。

- [ ] **Step 4: 跑确认通过 + 全量**

Run: `python -m pytest tests/test_notebook_share_readonly.py::test_list_notebooks_includes_joined_marked_reader -v && python -m pytest -q`
Expected: PASS;全量绿。

- [ ] **Step 5: 提交**

```bash
git add backend/app/models/schemas.py backend/app/services/sqlite_repository.py backend/tests/test_notebook_share_readonly.py
git commit -m "feat(share-p2): list_notebooks 合并自有∪加入 + access/shared_from 标记"
```

---

## Task 6: join / leave 端点

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（`join_shared`/`leave_notebook`）
- Modify: `backend/app/api/routes.py`（两端点)
- Test: `backend/tests/test_notebook_share_readonly.py`

**Interfaces:**
- Consumes: `find_notebook_by_share_token`(Phase 1)、`notebook_copy_stats`、`add_member`/`remove_member`。
- Produces: `POST /shared/{token}/join`→`NotebookSummary`(access=reader);`DELETE /notebooks/{id}/membership`→204。

- [ ] **Step 1: 写失败测试**

```python
def test_join_large_then_leave(tmp_path, monkeypatch, repo):
    # ⚠ 必须在任何 HTTP 请求(触发 repository() 首次构建+缓存)之前设阈值,否则 app 缓存旧值→大库被判小库
    monkeypatch.setenv("NOTEBOOK_COPY_MAX_ROWS", "1")
    client = _client(tmp_path, monkeypatch)
    owner_h = _login(client, "a00000011")   # 首个请求:此时 repository() 才构建,读到 MAX_ROWS=1
    nb = client.post("/api/notebooks", json={"name": "Big"}, headers=owner_h).json()["id"]
    with repo._write() as db:  # 造大库(3 个节点 > 阈值 1 → readonly)
        for i in range(3):
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,created_at,updated_at) "
                       "VALUES (?,?,?,?,?)", (f"ko-{i}", nb, "concept", _now(), _now()))
    token = client.post(f"/api/notebooks/{nb}/share", headers=owner_h).json()["share_token"]
    bob_h = _login(client, "b00000012")
    joined = client.post(f"/api/shared/{token}/join", headers=bob_h)
    assert joined.status_code == 200 and joined.json()["access"] == "reader"
    ids = {n["id"]: n for n in client.get("/api/notebooks", headers=bob_h).json()}
    assert nb in ids and ids[nb]["access"] == "reader"
    assert client.request("DELETE", f"/api/notebooks/{nb}/membership", headers=bob_h).status_code == 204
    assert nb not in {n["id"] for n in client.get("/api/notebooks", headers=bob_h).json()}
```

- [ ] **Step 2: 跑确认失败**

Run: `python -m pytest tests/test_notebook_share_readonly.py::test_join_large_then_leave -v`
Expected: FAIL(端点不存在)

- [ ] **Step 3: 实现**

`sqlite_repository.py` 加:

```python
    def join_shared(self, notebook_id: str, user_id: str) -> "NotebookSummary":
        """把 user 加为只读成员(幂等),返回该库 summary(access=reader)。"""
        self.add_member(notebook_id, user_id)
        with self._connect() as db:
            row = db.execute("SELECT * FROM notebooks WHERE id=?", (notebook_id,)).fetchone()
            nb = self._notebook_from_row(db, row)
        nb.access = "reader"
        return nb

    def leave_notebook(self, notebook_id: str, user_id: str) -> None:
        self.remove_member(notebook_id, user_id)
```

`routes.py` 加两端点(挂 `get_current_user`;join 经 token,leave 只动自己):

```python
@router.post("/shared/{token}/join", response_model=NotebookSummary)
def join_shared_route(token: str, user: UserProfile = Depends(get_current_user)) -> NotebookSummary:
    repo = repository()
    nb_id = repo.find_notebook_by_share_token(token)
    if nb_id is None:
        raise HTTPException(status_code=404, detail="Shared notebook not found")
    if repo.notebook_copy_stats(nb_id)["copyable"]:
        raise HTTPException(status_code=400, detail="small notebook — use copy, not join")
    return repo.join_shared(nb_id, user.id)


@router.delete("/notebooks/{notebook_id}/membership", status_code=204)
def leave_notebook_route(notebook_id: str, user: UserProfile = Depends(get_current_user)) -> None:
    repository().leave_notebook(notebook_id, user.id)
```

- [ ] **Step 4: 跑确认通过 + 全量**

Run: `python -m pytest tests/test_notebook_share_readonly.py::test_join_large_then_leave -v && python -m pytest -q`
Expected: PASS;全量绿。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/app/api/routes.py backend/tests/test_notebook_share_readonly.py
git commit -m "feat(share-p2): join/leave 端点(大库只读加入 + 退出)"
```

---

## Task 7: owner「已分享总览」端点

**Files:**
- Modify: `backend/app/models/schemas.py`（`SharedByMeItem`)
- Modify: `backend/app/services/sqlite_repository.py`（`shared_by_me`)
- Modify: `backend/app/api/routes.py`（`GET /notebooks/shared-by-me`)
- Test: `backend/tests/test_notebook_share_readonly.py`

**Interfaces:**
- Consumes: `notebook_copy_stats`、`list_members`。
- Produces: `GET /notebooks/shared-by-me`→`List[SharedByMeItem]`。

- [ ] **Step 1: 写失败测试**

```python
def test_shared_by_me_lists_with_members(repo):
    small = _mk_nb(repo, owner="user-local", name="Small")
    big = _mk_nb(repo, owner="user-local", name="Big")
    other = _mk_nb(repo, owner="user-alice", name="Other")
    _mk_user(repo, "user-bob", "b00000013")
    repo.share_notebook(small); repo.share_notebook(big); repo.share_notebook(other)
    with repo._write() as db:
        for i in range(3):
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,created_at,updated_at) "
                       "VALUES (?,?,?,?,?)", (f"ko-{i}", big, "concept", _now(), _now()))
    repo.settings.notebook_copy_max_rows = 1  # big→readonly
    repo.add_member(big, "user-bob")
    items = {it["id"]: it for it in repo.shared_by_me("user-local")}
    assert set(items) == {small, big}              # 只我 owner 的、且 is_shared;不含 alice 的
    assert items[big]["mode"] == "readonly"
    assert [m["username"] for m in items[big]["members"]] == ["b00000013"]
    assert items[small]["members"] == []           # 小库(copy)无成员
```

- [ ] **Step 2: 跑确认失败**

Run: `python -m pytest tests/test_notebook_share_readonly.py::test_shared_by_me_lists_with_members -v`
Expected: FAIL

- [ ] **Step 3: 实现**

`schemas.py` 加:

```python
class SharedByMeItem(BaseModel):
    id: str
    name: str
    share_token: str
    mode: str                      # "copy" | "readonly"
    size: Dict[str, int]
    members: List[Dict[str, str]]  # [{username, added_at}]
```

`sqlite_repository.py` 加:

```python
    def shared_by_me(self, user_id: str) -> list:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, name, share_token FROM notebooks "
                "WHERE created_by=? AND is_shared=1 ORDER BY updated_at DESC", (user_id,)).fetchall()
        out = []
        for r in rows:
            stats = self.notebook_copy_stats(r["id"])
            readonly = not stats["copyable"]
            out.append({
                "id": r["id"], "name": r["name"], "share_token": r["share_token"] or "",
                "mode": "readonly" if readonly else "copy", "size": stats["size"],
                "members": self.list_members(r["id"]) if readonly else [],
            })
        return out
```

`routes.py` 加(**放在 `/notebooks/{notebook_id}` 动态路由之前**避免被吞——FastAPI 按定义序匹配,`shared-by-me` 是静态段):

```python
@router.get("/notebooks/shared-by-me", response_model=List[SharedByMeItem])
def shared_by_me_route(user: UserProfile = Depends(get_current_user)) -> List[SharedByMeItem]:
    return [SharedByMeItem(**it) for it in repository().shared_by_me(user.id)]
```

> **路由顺序坑**:`/notebooks/shared-by-me` 必须在 `@router.get("/notebooks/{notebook_id}")`(:228)**之前**注册,否则 `shared-by-me` 被当作 `{notebook_id}` 吞掉。放到 `list_notebooks`(:218)之后、`get_notebook` 之前。

- [ ] **Step 4: 跑确认通过 + 全量**

Run: `python -m pytest tests/test_notebook_share_readonly.py::test_shared_by_me_lists_with_members -v && python -m pytest -q`
Expected: PASS;全量绿。

- [ ] **Step 5: 提交**

```bash
git add backend/app/models/schemas.py backend/app/services/sqlite_repository.py backend/app/api/routes.py backend/tests/test_notebook_share_readonly.py
git commit -m "feat(share-p2): GET /notebooks/shared-by-me owner 已分享总览"
```

---

## Task 8: 前端 client — join/leave/sharedByMe + preview readonly

**Files:**
- Modify: `frontend/app/notebook-share.ts`
- Test: `frontend/app/notebook-share.test.mjs`

**Interfaces:**
- Produces: `joinShared(token)`、`leaveNotebook(id)`、`sharedByMe()`、类型 `SharedByMeItem`;`SharedPreview.mode` 加 `"readonly"`。

- [ ] **Step 1: 写失败测试(纯 helper——加一个 mode 文案 helper 便于单测)**

在 `notebook-share.test.mjs` 加:

```javascript
import { shareModeLabel } from "./notebook-share.ts";
test("shareModeLabel", () => {
  assert.equal(shareModeLabel("copy"), "可拷贝");
  assert.equal(shareModeLabel("readonly"), "只读共享");
});
```

- [ ] **Step 2: 跑确认失败**

Run: `cd frontend && node --test app/notebook-share.test.mjs`
Expected: FAIL(`shareModeLabel` 未导出)

- [ ] **Step 3: 实现**

`notebook-share.ts`:`SharedPreview.mode` 类型改 `"copy" | "readonly"`;加:

```typescript
export type SharedByMeItem = {
  id: string; name: string; share_token: string;
  mode: "copy" | "readonly"; size: ShareSize;
  members: { username: string; added_at: string }[];
};

export const joinShared = (token: string): Promise<NotebookSummaryLike> =>
  apiFetch(`/shared/${token}/join`, { method: "POST" });

export const leaveNotebook = (notebookId: string): Promise<void> =>
  apiFetch<void>(`/notebooks/${notebookId}/membership`, { method: "DELETE" });

export const sharedByMe = (): Promise<SharedByMeItem[]> =>
  apiFetch(`/notebooks/shared-by-me`);

export const shareModeLabel = (mode: string): string =>
  mode === "readonly" ? "只读共享" : "可拷贝";
```

- [ ] **Step 4: 跑确认通过**

Run: `cd frontend && node --test app/notebook-share.test.mjs`
Expected: `# pass 7`(6 旧 + 1 新)

- [ ] **Step 5: 提交**

```bash
git add frontend/app/notebook-share.ts frontend/app/notebook-share.test.mjs
git commit -m "feat(share-p2): 前端 client join/leave/sharedByMe + readonly 文案"
```

---

## Task 9: 前端 page.tsx — 只读徽章 + 写按钮门控 + 加入/退出 + 已分享总览

**Files:**
- Modify: `frontend/app/page.tsx`
- Test: 无(UI;靠 tsc + 人工视觉)

**Interfaces:**
- Consumes: Task 8 的 `joinShared`/`leaveNotebook`/`sharedByMe`/`shareModeLabel`/`SharedByMeItem`、Phase 1 的预览弹窗。

- [ ] **Step 1: 只读门控 helper + 徽章**
`currentNotebook?.access === "reader"` 记为 `isReader`。在 notebook 头部标题区渲染徽章(access==="reader" 时):`<span className="pill">只读 · 来自 {currentNotebook.shared_from}</span>`(用现有 pill/badge class)。

- [ ] **Step 2: 门控写按钮**
把这些按钮/入口在 `isReader` 时 `disabled` 或不渲染:分享(Phase 1 的分享按钮)、添加来源、删除 notebook、刷新图谱/建 KG、分析弹窗里的治理动作(晋升/基准库/边审查)、标题重命名(改为只读文本)。做法:给这些 `<button>` 加 `disabled={isReader}`(或 `{!isReader && (...)}` 包裹),并加 `title="只读共享,无写权限"`。**保留**:提问输入、会话、来源浏览、知识图谱查看。

- [ ] **Step 3: 预览弹窗加 readonly 分支**
Phase 1 的接收预览弹窗(`sharedPreview`)加:`mode==="readonly"` 时按钮为「加入(只读)」→ `joinShared(shareTokenRef.current)` → `loadNotebookCollection` → `setCurrentNotebookId(新.id)` → 关闭 + toast「已加入只读共享」。`mode==="copy"` 维持 Phase 1 的「拷贝到我的空间」。

- [ ] **Step 4: reader 库「退出共享」入口**
`isReader` 时在头部给「退出共享」按钮 → `leaveNotebook(currentNotebook.id)` → `loadNotebookCollection` → 若当前选中被移除则切到第一个自有库 → toast。

- [ ] **Step 5: 「已分享」总览 modal**
在合适入口(如 notebook 列表顶或用户菜单)加「已分享」按钮 → `sharedByMe()` → 打开 `utility-modal`:列表每项 = 名称 + `shareModeLabel(mode)` 徽章 + 只读链接 input + 复制按钮(复用 `buildShareLink`)+ 规模;`mode==="readonly"` 展示 `members.map(m=>m.username)`(逗号连,空则「暂无成员」);每项「取消分享」按钮 → `unshareNotebook(id)` → 刷新总览(重调 sharedByMe)+ 若该库当前打开则从列表消失。用 Phase 1 分享 modal 的样式。

- [ ] **Step 6: 验证 + 提交**

Run: `cd frontend && npx tsc --noEmit`
Expected: clean。检查弯引号:`git diff -- app/page.tsx | grep -c '^-.*[""]'` 应为 0(除非替换了既有含引号行,需人工确认是有意)。

```bash
git add frontend/app/page.tsx
git commit -m "feat(share-p2): 前端 只读徽章+写按钮门控+加入/退出+已分享总览"
```

> 视觉走查(需后端+登录,留给用户):owner 分享大库→「已分享」看到它+成员;bob 开 ?share= 链接→预览 readonly→加入→列表现只读徽章、写按钮禁用、能问答;bob 退出→消失;owner 取消分享→bob 列表消失。

---

## 收尾(全部 Task 完成后)

- 全量 `python -m pytest -q` 绿 + `frontend` `npx tsc --noEmit` clean + `node --test app/notebook-share.test.mjs` 绿。
- 已在 `feat/notebook-share-copy` 分支(PR#127);push 更新该 PR,PR 描述补 Phase 2 段(大库只读共享 + 已分享总览)。真机四态走查留用户。
