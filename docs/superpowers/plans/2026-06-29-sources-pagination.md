# 来源分页(Part B)实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 让单个 notebook 有上万个 source 时,来源列表 API/前端不再一次性加载全部;并修掉 `search_notebook` 的全量加载 OOM。

**Architecture:** 后端**新增** `list_sources_page(notebook_id, offset, limit, q)` 返回 `PaginatedSources{items,total_count,offset,limit}`(不改 `list_sources`,后者仍供 `reextract` 全量迭代);现有 `GET /sources` 改为分页(唯一前端调用点同步改)。`search_notebook` 改为 SQL 侧 `LOWER(col) LIKE ?` + 每实体 `LIMIT`(替代 load-all-then-filter)。前端来源面板首屏分页取数 + 搜索框 + 「加载更多」。

**Tech Stack:** FastAPI + pydantic v2 + SQLite;Next.js/React/TS 前端;pytest;`PYTHONPATH=backend` + 本机 conda python。

> 范围只此 Part B(spec §5)。基于 master `b569843`(#94/#95 已合)。

---

## 文件结构

| 文件 | 改动 |
|---|---|
| Modify `backend/app/models/schemas.py` | 新增 `PaginatedSources` |
| Modify `backend/app/services/repository.py` | Protocol 加 `list_sources_page` |
| Modify `backend/app/services/sqlite_repository.py` | 实现 `list_sources_page`;重写 `search_notebook`(SQL LIKE+LIMIT) |
| Modify `backend/app/api/routes.py` | `GET /sources` 改分页(offset/limit/q) |
| Modify `frontend/app/page.tsx` | `openNotebook` 分页取数 + 搜索框 + 加载更多 + 计数用 total |
| Test `backend/tests/test_sources_pagination.py` | 新增:repo 分页 + search OOM 修 + 路由 |

**规范签名(全程一致):**
```python
class PaginatedSources(BaseModel):
    items: List[SourceSummary]; total_count: int; offset: int; limit: int
list_sources_page(self, notebook_id: str, offset: int = 0, limit: int = 50, q: str = "") -> PaginatedSources
```

---

## Task 1: PaginatedSources schema + repo 分页方法 + Protocol

**Files:** Modify `schemas.py`, `repository.py`, `sqlite_repository.py`; Test `backend/tests/test_sources_pagination.py`

- [ ] **Step 1: 写失败测试(测试文件头 + repo 分页用例)**

```python
# backend/tests/test_sources_pagination.py
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, _now
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _seed_sources(repo, nb_id, n, prefix="Doc"):
    now = _now()
    with repo._write() as db:
        for i in range(n):
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
                "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"src-{i:04d}", nb_id, f"{prefix} {i:04d}", "document", f"f{i}.md",
                 f"/tmp/f{i}.md", 0, f"h{i}", "", "", "extracted",
                 f"2026-01-01T00:00:{i:02d}", now))


def test_list_sources_page_paginates(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_sources(repo, nb.id, 130)
    page = repo.list_sources_page(nb.id, offset=0, limit=50)
    assert page.total_count == 130
    assert len(page.items) == 50
    assert page.offset == 0 and page.limit == 50
    assert page.items[0].title == "Doc 0000"        # ORDER BY created_at ASC
    page2 = repo.list_sources_page(nb.id, offset=100, limit=50)
    assert len(page2.items) == 30                    # 末页
    assert page2.items[0].title == "Doc 0100"


def test_list_sources_page_query_filters(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_sources(repo, nb.id, 20, prefix="Alpha")
    _seed_sources(repo, nb.id, 5, prefix="Beta")     # 注意:id 会与上批冲突? 用不同前缀的独立 nb 更稳
    # 重新用干净 nb 避免 id 冲突
    nb2 = repo.create_notebook(NotebookCreate(name="nb2"))
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
                   "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("src-a", nb2.id, "Voltage Reference", "document", "vref.md", "/tmp/vref.md",
                    0, "ha", "", "", "extracted", "2026-01-01T00:00:00", _now()))
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
                   "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("src-b", nb2.id, "Clock Tree", "document", "clk.md", "/tmp/clk.md",
                    0, "hb", "", "", "extracted", "2026-01-01T00:00:01", _now()))
    page = repo.list_sources_page(nb2.id, q="voltage")        # 大小写不敏感、按 title
    assert page.total_count == 1 and page.items[0].id == "src-a"
    page_fn = repo.list_sources_page(nb2.id, q="clk.md")      # 按 file_name
    assert page_fn.total_count == 1 and page_fn.items[0].id == "src-b"
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_sources_pagination.py -k list_sources_page -v`
Expected: FAIL — `AttributeError: 'SQLiteRepository' object has no attribute 'list_sources_page'`

- [ ] **Step 3a: schemas.py 新增 `PaginatedSources`(放在 `SourceSummary` 类定义之后)**

```python
class PaginatedSources(BaseModel):
    items: List[SourceSummary]
    total_count: int
    offset: int
    limit: int
```

- [ ] **Step 3b: repository.py Protocol 加方法(在 `list_sources` 那行下面),并确保 `PaginatedSources` 已 import**

在 `from app.models.schemas import (...)` 列表里加入 `PaginatedSources`;Protocol 内 `list_sources` 行之后加:
```python
    def list_sources_page(self, notebook_id: str, offset: int = 0, limit: int = 50, q: str = "") -> PaginatedSources: ...
```

- [ ] **Step 3c: sqlite_repository.py 实现(放在 `list_sources` 方法之后),并确保 `PaginatedSources` 已在该文件 import**

```python
    def list_sources_page(self, notebook_id: str, offset: int = 0, limit: int = 50,
                          q: str = "") -> PaginatedSources:
        """分页 + 可选 q(按 title/file_name 服务端过滤)。万级 source 安全:只取一页 +
        一次 COUNT,不全量进内存。"""
        self.get_notebook(notebook_id)
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 200))
        needle = (q or "").strip().lower()
        where = "WHERE notebook_id = ?"
        params: List[object] = [notebook_id]
        if needle:
            where += " AND (LOWER(title) LIKE ? OR LOWER(file_name) LIKE ?)"
            like = f"%{needle}%"
            params += [like, like]
        with self._connect() as db:
            total = db.execute(
                f"SELECT COUNT(*) c FROM sources {where}", params).fetchone()["c"]
            rows = db.execute(
                f"SELECT * FROM sources {where} ORDER BY created_at ASC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            items = [self._source_from_row(db, row) for row in rows]
        return PaginatedSources(items=items, total_count=total, offset=offset, limit=limit)
```

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_sources_pagination.py -k list_sources_page -v`
Expected: PASS (2 passed)

- [ ] **Step 5: 提交**

```bash
git add backend/app/models/schemas.py backend/app/services/repository.py \
        backend/app/services/sqlite_repository.py backend/tests/test_sources_pagination.py
git commit -m "feat(sources): 分页 repo 方法 list_sources_page + PaginatedSources"
```

---

## Task 2: `GET /sources` 路由改分页

**Files:** Modify `backend/app/api/routes.py`; Test `backend/tests/test_sources_pagination.py`(追加)

- [ ] **Step 1: 追加路由测试(TestClient,auth_optional 由 conftest 默认开)**

```python
def test_get_sources_route_paginates(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.core.config import get_settings
    from app.api import deps
    get_settings.cache_clear(); deps.repository.cache_clear()
    from fastapi.testclient import TestClient
    from app.main import create_app
    client = TestClient(create_app())
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()
    repo = deps.repository()
    _seed_sources(repo, nb["id"], 60)
    r = client.get(f"/api/notebooks/{nb['id']}/sources?offset=0&limit=25")
    assert r.status_code == 200
    body = r.json()
    assert body["total_count"] == 60 and len(body["items"]) == 25
    assert body["offset"] == 0 and body["limit"] == 25
    get_settings.cache_clear(); deps.repository.cache_clear()
```

- [ ] **Step 2: 运行确认失败**

Run: `PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_sources_pagination.py::test_get_sources_route_paginates -v`
Expected: FAIL — 现路由返回的是 list(无 `total_count` 键)→ KeyError/断言失败。

- [ ] **Step 3: 改路由(routes.py:265-267)**

确保文件顶部已从 fastapi import `Query`(通常已 import;若无则加)、并从 schemas import `PaginatedSources`。把:
```python
@router.get("/notebooks/{notebook_id}/sources", response_model=List[SourceSummary], dependencies=[Depends(require_notebook_access)])
def list_sources(notebook_id: str) -> List[SourceSummary]:
    return repository().list_sources(notebook_id)
```
改为:
```python
@router.get("/notebooks/{notebook_id}/sources", response_model=PaginatedSources, dependencies=[Depends(require_notebook_access)])
def list_sources(
    notebook_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    q: str = Query(""),
) -> PaginatedSources:
    return repository().list_sources_page(notebook_id, offset=offset, limit=limit, q=q)
```

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_sources_pagination.py -v`
Expected: PASS（全部）

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/routes.py backend/tests/test_sources_pagination.py
git commit -m "feat(sources): GET /sources 改分页(offset/limit/q + total_count)"
```

---

## Task 3: 修 `search_notebook` 全量加载 OOM

**Files:** Modify `backend/app/services/sqlite_repository.py`; Test `backend/tests/test_sources_pagination.py`(追加)

- [ ] **Step 1: 追加测试(验证:命中正确 + 大量元素时不全量、按 LIMIT)**

```python
def test_search_notebook_sql_filtered(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    now = _now()
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,"
                   "file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("src-1", nb.id, "Bandgap Reference", "document", "bg.md", "/tmp/bg.md",
                    0, "h", "", "", "extracted", now, now))
        # 200 个元素,只有 1 个含 needle —— 旧实现会把 200 个全读进内存
        for i in range(200):
            txt = "the curvature correction term" if i == 7 else f"unrelated paragraph {i}"
            db.execute("INSERT INTO source_elements (id,source_id,element_type,location_label,"
                       "text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
                       (f"el-{i:03d}", "src-1", "paragraph", f"p{i}", txt, "{}", now))
    resp = repo.search_notebook(nb.id, "curvature")
    assert any(h.element_id == "el-007" for h in resp.hits)
    resp_title = repo.search_notebook(nb.id, "bandgap")     # 命中 source title
    assert any(h.scope == "Source" for h in resp_title.hits)
    assert repo.search_notebook(nb.id, "").hits == []        # 空 query 短路
```

- [ ] **Step 2: 运行确认通过/失败基线**

Run: `PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_sources_pagination.py::test_search_notebook_sql_filtered -v`
Expected: 当前实现可能 PASS(功能正确,只是低效)。本任务是**重构去 OOM**,以该测试作回归护栏——重写后必须仍 PASS。

- [ ] **Step 3: 重写 `search_notebook`(把 `LIKE` 下推 SQL + 每实体 LIMIT;不再 `SELECT *` 全量)**

把 `search_notebook` 内 `with self._connect() as db:` 里的四条全量查询及随后的 `candidates` 构建,替换为按 needle 过滤 + LIMIT 的查询。完整替换该方法体为:
```python
    def search_notebook(self, notebook_id: str, query: str) -> NotebookSearchResponse:
        self.get_notebook(notebook_id)
        needle = query.strip().lower()
        if not needle:
            return NotebookSearchResponse(query=query, hits=[])
        like = f"%{needle}%"
        cap = 20  # 总命中上限(沿用旧 hits[:20]);每实体各取至多 cap,合并后再截断
        hits: List[SearchHit] = []
        with self._connect() as db:
            notebook = db.execute("SELECT * FROM notebooks WHERE id = ?", (notebook_id,)).fetchone()
            # Notebook / Domain:两条,Python 侧判断即可(无需查询)
            for scope, text in (("Notebook", notebook["name"]), ("Domain", notebook["primary_domain"])):
                if needle in f"{scope} {text}".lower():
                    hits.append(SearchHit(scope=scope, notebook_id=notebook_id, label=scope,
                                          text=_snippet(text or scope, needle), source_id="", element_id=""))
            src_rows = db.execute(
                "SELECT * FROM sources WHERE notebook_id = ? AND "
                "(LOWER(title) LIKE ? OR LOWER(summary) LIKE ? OR LOWER(file_name) LIKE ?) "
                "ORDER BY created_at ASC LIMIT ?",
                (notebook_id, like, like, like, cap)).fetchall()
            for s in src_rows:
                label = s["title"] or s["file_name"]
                body = s["summary"] or s["file_name"] or s["title"]
                hits.append(SearchHit(scope="Source", notebook_id=notebook_id, label=label,
                                      text=_snippet(body, needle), source_id=s["id"], element_id=""))
            el_rows = db.execute(
                "SELECT se.*, s.title AS source_title FROM source_elements se "
                "JOIN sources s ON s.id = se.source_id WHERE s.notebook_id = ? AND "
                "(LOWER(se.text) LIKE ? OR LOWER(se.location_label) LIKE ? OR LOWER(s.title) LIKE ?) "
                "LIMIT ?",
                (notebook_id, like, like, like, cap)).fetchall()
            for e in el_rows:
                label = f"{e['source_title']} · {e['location_label']}"
                hits.append(SearchHit(scope="Element", notebook_id=notebook_id, label=label,
                                      text=_snippet(e["text"] or label, needle),
                                      source_id=e["source_id"], element_id=e["id"]))
            art_rows = db.execute(
                "SELECT * FROM articles WHERE notebook_id = ? AND "
                "(LOWER(title) LIKE ? OR LOWER(summary) LIKE ?) LIMIT ?",
                (notebook_id, like, like, cap)).fetchall()
            for a in art_rows:
                hits.append(SearchHit(scope="Article", notebook_id=notebook_id, label=a["title"],
                                      text=_snippet(a["summary"] or a["title"], needle),
                                      source_id="", element_id=""))
            ko_rows = db.execute(
                "SELECT id, object_type, payload FROM knowledge_objects "
                "WHERE notebook_id = ? AND status != 'deprecated' AND LOWER(payload) LIKE ? LIMIT ?",
                (notebook_id, like, cap)).fetchall()
            for ko in ko_rows:
                payload = json.loads(ko["payload"] or "{}")
                label = OBJECT_TYPE_LABELS.get(ko["object_type"], ko["object_type"])
                headline = self._knowledge_headline(ko["object_type"], payload)
                body = self._payload_join(payload)
                if needle not in f"{label} {headline} {body}".lower():
                    continue  # payload LIKE 命中但去掉键名/无关字段的假阳
                hits.append(SearchHit(scope=label, notebook_id=notebook_id, label=headline,
                                      text=_snippet(body or headline, needle), source_id="", element_id=""))
        return NotebookSearchResponse(query=query, hits=hits[:20])
```

- [ ] **Step 4: 运行确认通过**

Run: `PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_sources_pagination.py -v`
Expected: PASS（含 search 用例）

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_sources_pagination.py
git commit -m "fix(search): search_notebook 改 SQL LIKE+LIMIT,去全量加载 OOM"
```

---

## Task 4: 前端来源面板分页 + 搜索 + 加载更多

**Files:** Modify `frontend/app/page.tsx`

> 上下文:`SourceSummary` 类型在 page.tsx:76;状态在 ~824(`const [sources,setSources]=useState<SourceSummary[]>([])`);`openNotebook` 在 1382;来源面板渲染在 ~2585(头部计数 2588、`.source-list` 内 `sources.map` 2632);`api<T>(path)` 在 492。后端现返回 `{items,total_count,offset,limit}`。

- [ ] **Step 1: 新增类型 + 状态**

在 `SourceSummary` 类型定义(page.tsx:76 起的 `type SourceSummary = {...}`)之后加:
```typescript
type PaginatedSources = {
  items: SourceSummary[];
  total_count: number;
  offset: number;
  limit: number;
};
const SOURCES_PAGE_SIZE = 50;
```
在 `const [sources, setSources] = useState<SourceSummary[]>([]);`(~824)之后加:
```typescript
  const [sourcesTotal, setSourcesTotal] = useState(0);
  const [sourceQuery, setSourceQuery] = useState("");
```

- [ ] **Step 2: 取数助手 + openNotebook 改分页**

在组件内(`openNotebook` 之前)新增助手:
```typescript
  async function loadSourcesPage(notebookId: string, opts: { reset?: boolean; query?: string } = {}) {
    const q = opts.query ?? sourceQuery;
    const offset = opts.reset ? 0 : sources.length;
    const page = await api<PaginatedSources>(
      `/notebooks/${notebookId}/sources?offset=${offset}&limit=${SOURCES_PAGE_SIZE}&q=${encodeURIComponent(q)}`,
    );
    setSourcesTotal(page.total_count);
    setSources((prev) => (opts.reset ? page.items : [...prev, ...page.items]));
  }
```
把 `openNotebook` 里的 sources 取数从一次性改为首屏分页。将:
```typescript
    const [notebook, notebookSources, notebookArticles] = await Promise.all([
      api<NotebookSummary>(`/notebooks/${notebookId}`),
      api<SourceSummary[]>(`/notebooks/${notebookId}/sources`),
      api<ArticleSummary[]>(`/notebooks/${notebookId}/articles`)
    ]);
```
改为:
```typescript
    const [notebook, sourcesPage, notebookArticles] = await Promise.all([
      api<NotebookSummary>(`/notebooks/${notebookId}`),
      api<PaginatedSources>(`/notebooks/${notebookId}/sources?offset=0&limit=${SOURCES_PAGE_SIZE}`),
      api<ArticleSummary[]>(`/notebooks/${notebookId}/articles`)
    ]);
```
并把随后的 `setSources(notebookSources);` 改为:
```typescript
    setSources(sourcesPage.items);
    setSourcesTotal(sourcesPage.total_count);
    setSourceQuery("");
```

- [ ] **Step 3: 维护 total(上传/删除时)**

- 上传合并处(~1605-1609,`const uploaded = await api<SourceSummary[]>(.../sources, {POST...})` 之后的 `setSources((previous) => [...])`):在其后加 `setSourcesTotal((t) => t + uploaded.length);`(新上传计入总数;dup 复传的轻微高估可接受)。
- 删除处(~1683,`setSources((previous) => previous.filter((item) => item.id !== source.id));`):在其后加 `setSourcesTotal((t) => Math.max(0, t - 1));`。

- [ ] **Step 4: 头部计数用 total + 搜索框 + 加载更多**

- 头部计数(2588)`{sources.length} 个来源` → `{sourcesTotal} 个来源`。
- 在 `.source-list`(2624)**之前**插入搜索框:
```tsx
                <input
                  className="source-search"
                  type="search"
                  placeholder="搜索来源（标题/文件名）"
                  value={sourceQuery}
                  onChange={(e) => setSourceQuery(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && currentNotebookId) {
                      loadSourcesPage(currentNotebookId, { reset: true, query: sourceQuery }).catch(reportError);
                    }
                  }}
                />
```
- 在 `.source-list` 内 `sources.map(...)` 渲染**之后**(`)}` 闭合 map 之后、`</div>` 关闭 `.source-list` 之前)加「加载更多」:
```tsx
                  {sources.length > 0 && sources.length < sourcesTotal && (
                    <button
                      type="button"
                      className="add-source-button"
                      onClick={() => { if (currentNotebookId) loadSourcesPage(currentNotebookId).catch(reportError); }}
                    >
                      加载更多（{sources.length}/{sourcesTotal}）
                    </button>
                  )}
```

- [ ] **Step 5: tsc + lint**

Run: `cd frontend && npm run lint`
Expected: 0 errors（tsc 干净）。若 `reportError` 不存在,用既有错误处理写法(搜既有 `.catch(` 用法对齐)。

- [ ] **Step 6: 提交**

```bash
git add frontend/app/page.tsx
git commit -m "feat(sources): 前端来源面板分页(加载更多)+ 服务端搜索框"
```

---

## Task 5: 全量验证 + PR

- [ ] **Step 1: 本特性测试**

Run: `PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_sources_pagination.py -v`
Expected: 全 PASS。

- [ ] **Step 2: 门禁 + 回归**

Run: `scripts/check.sh`
Expected: exit 0（py_compile + smoke + ask-mode contract + 前端 test/lint;worktree 有 frontend/node_modules 时跑前端,否则跳过——若跳过则单独 `cd frontend && npm run lint`)。
Run(回归后端全量): `PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests -q`
Expected: 仅既有预存失败(`test_kg_quality::test_offline_clis_parse` + `test_innovus_characterization` 3 个,缺本地源 fixture),无本特性新增失败。

- [ ] **Step 3: 提 PR(rebase 到 master 保持线性)**

```bash
git fetch origin && git rebase origin/master
git push -u origin claude/sources-pagination
gh pr create --base master --title "feat(sources): 来源分页 + search_notebook OOM 修(Part B)" \
  --body "spec §5。GET /sources 分页(offset/limit/q + total_count,新增 list_sources_page 不动 list_sources)、search_notebook 改 SQL LIKE+LIMIT 去全量 OOM、前端来源面板加载更多+服务端搜索。"
```

---

## Self-Review(计划 vs spec §5)

- **后端分页**(offset/limit + total_count 包装)→ Task 1+2。**新增** `list_sources_page` 不改 `list_sources`(保护 `reextract` 唯一内部调用方),偏离 spec「修改 list_sources 签名」字面但更安全。
- **search_notebook OOM**(SQL LIKE+LIMIT,不再全量进内存)→ Task 3,带回归测试。
- **前端**(首屏分页 + 加载更多 + 搜索框;计数用 total)→ Task 4。引用不受影响(后端 citation.label 已含标题,前端引用渲染不依赖全量 sources——spec §5.4 已核实,无需改)。
- **q 服务端搜索**:面板搜索走后端 `q`(SQL LIKE 标题/文件名),适配万级(不只过滤已加载页)。
- **Placeholder/一致性**:`PaginatedSources` 字段、`list_sources_page` 签名在 schema/Protocol/impl/route/前端一致;无 TODO。
- **YAGNI**:不引入虚拟滚动(先「加载更多」+ 搜索;真机仍卡再议);不做 cursor 分页(offset 足够,来源按 created_at 稳定排序)。
