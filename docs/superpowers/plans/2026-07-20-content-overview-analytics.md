# Content Overview Analytics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add viewer-aware Memory and Knowhow cards to the existing notebook analytics view while making every user-facing source count mean user-imported sources only.

**Architecture:** A focused `ContentOverviewService` composes two consumer-specific store ports: Memory supplies a current-user/current-notebook aggregate, and Knowhow supplies one fixed-query batched health projection whose freshness calculation reuses the canonical Knowhow hash. A dedicated read endpoint exposes the typed result without changing the existing notebook-wide analytics contract. Existing Memory and Knowhow panels remain the only browse/edit surfaces; analytics sends typed navigation targets into them.

**Tech Stack:** Python 3.13, FastAPI, Pydantic v2, standard-library `sqlite3`, pytest, React 19, TypeScript, Next.js, Node test runner, Vitest, React Testing Library.

## Global Constraints

- Work only in `/Users/hzf/workspace/silicon_notebook/.worktrees/content-overview-analytics` on branch `codex/content-overview-analytics`.
- Ship the specialty as one pull request; intermediate task commits remain reviewable and may be squash-merged.
- Use `/opt/homebrew/Caskroom/miniconda/base/bin/python` for backend checks.
- The local warm `scripts/check.sh` full gate must remain at or below 60 seconds.
- Ubuntu GitHub Actions must be green; record CI duration but do not make CI duration a hard merge gate.
- Do not enable a required branch-protection check.
- Do not add Memory or Knowhow to the source rail or the four notebook workspace tabs.
- Do not add a second Memory or Knowhow browser/editor; analytics cards contain metrics, recent entries, and navigation only.
- Memory analytics is always restricted to the authenticated user and requested notebook. Admin cross-user Memory analytics is out of scope.
- Knowhow analytics follows existing notebook read access; destination pages retain their existing write restrictions.
- User-facing source counts exclude `source_type IN ('memory', 'knowhow')`; physical copy/storage and internal scheduler counters retain physical-row semantics.
- Store methods use a bounded constant number of queries. Do not add a per-Memory, per-table, per-row, or per-cell query.
- Reuse `app.services.knowhow.api.cell_content_hash` for stale-code derivation. Do not reproduce the hash algorithm in the endpoint or frontend.
- Preserve the line-pinned repository contracts: append new port and dependency declarations at end of their files, and append route registration at the end of `backend/app/api/routes.py`.
- Do not add a schema migration unless an `EXPLAIN QUERY PLAN` result proves the existing indexes cannot support the fixed aggregation queries.
- Do not add tests based on source-code line counts or implementation file length.
- Keep `README.md`, `README_zh.md`, and `AGENTS.md` synchronized, and update `fangan_done.md` for the completed analytics capability.
- After creating the PR, dispatch an independent subagent to review the exact pushed head; resolve all Critical and Important findings before handoff.

## File Responsibility Map

- `backend/app/services/content_overview.py`: application-level assembly of Memory and Knowhow analytics; sole owner of stale-code and recent-table derivation.
- `backend/app/api/content_overview_routes.py`: authenticated read route only.
- `backend/app/repositories/sqlite/memory_store.py`: two-query owner/notebook Memory aggregate.
- `backend/app/repositories/sqlite/knowhow_store.py`: four-query batched table/row/cell/code health input.
- `backend/app/repositories/ports.py`: consumer-specific protocols for the service.
- `backend/app/models/schemas.py`: wire models and additive Knowhow/scale status fields.
- `backend/app/repositories/sqlite/knowledge_counts_cache.py`: separate cached physical and visible pending-source counts.
- `backend/app/repositories/sqlite/query_store.py`, `backend/app/services/notebook_catalog.py`: user-facing notebook summary source semantics.
- `backend/app/repositories/sqlite/index_projection_store.py`, `backend/app/services/scale_artifact_runtime.py`, `backend/app/services/notebook_sharing.py`: visible scale/share counts while retaining physical decisions.
- `frontend/app/content-overview-cards.tsx`: render-only Memory/Knowhow analytics cards.
- `frontend/app/analytics-loaders.ts`: starts analytics/index/content reads together and settles optional reads independently.
- `frontend/app/memory-model.ts`, `frontend/app/memory-panel.tsx`: typed global Memory deep-link restoration using the existing list/detail UI.
- `frontend/app/knowhow-model.ts`, `frontend/app/knowhow-panel.tsx`: typed table-health filters using existing table cards/editor.
- `frontend/app/page.tsx`: parallel analytics loading and navigation ownership.
- `frontend/app/scale-index.ts`, `frontend/app/kg-build-status.ts`: visible-source wording and derived-content action semantics.

---

### Task 1: Add the typed content-overview service and fixed-query store projections

**Files:**
- Create: `backend/app/services/content_overview.py`
- Create: `backend/tests/test_content_overview_service.py`
- Modify: `backend/app/models/schemas.py`
- Modify: `backend/app/repositories/ports.py`
- Modify: `backend/app/repositories/sqlite/memory_store.py`
- Modify: `backend/app/repositories/sqlite/knowhow_store.py`
- Modify: `backend/tests/test_knowhow_store.py`

**Interfaces:**
- Consumes: `app.services.knowhow.api.cell_content_hash(content_md: str | None) -> str`.
- Produces: `ContentOverviewMemoryStorePort.notebook_content_overview(user_id: str, notebook_id: str, limit: int = 3) -> dict[str, Any]`.
- Produces: `ContentOverviewKnowhowStorePort.knowhow_table_health_inputs(notebook_id: str) -> list[dict[str, Any]]`.
- Produces: `ContentOverviewService.knowhow_tables(notebook_id: str) -> list[KnowhowTableSummary]`.
- Produces: `ContentOverviewService.get(notebook_id: str, user_id: str) -> NotebookContentOverview`.
- Produces: additive `KnowhowTableSummary.projection_pending`, `projection_failed`, `stale_code_count`, and `last_activity_at`.

- [ ] **Step 1: Write failing service tests for Memory privacy, Knowhow health, recency, and empty results**

Create `backend/tests/test_content_overview_service.py` with fake ports whose return values are explicit. The assertions pin lifecycle semantics, canonical stale detection, descending activity order, and the three-row bound:

```python
from app.services.knowhow.api import cell_content_hash
from app.services.content_overview import ContentOverviewService


class FakeMemoryStore:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def notebook_content_overview(self, user_id, notebook_id, limit=3):
        self.calls.append((user_id, notebook_id, limit))
        return self.result


class FakeKnowhowStore:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def knowhow_table_health_inputs(self, notebook_id):
        self.calls.append(notebook_id)
        return self.rows


def test_content_overview_uses_viewer_memory_and_canonical_code_freshness():
    memory = FakeMemoryStore({
        "total": 4,
        "confirmed": 2,
        "candidate": 1,
        "recent": [{
            "id": "m1",
            "title": "Stable memory",
            "status": "confirmed",
            "updated_at": "2026-07-20T08:00:00+00:00",
        }],
    })
    knowhow = FakeKnowhowStore([{
        "id": "t1",
        "notebook_id": "nb1",
        "title": "Bring-up",
        "description": "",
        "created_at": "2026-07-19T00:00:00+00:00",
        "updated_at": "2026-07-19T01:00:00+00:00",
        "row_count": 2,
        "projection_pending": 1,
        "projection_failed": 1,
        "row_activity_at": "2026-07-20T06:00:00+00:00",
        "cell_activity_at": "2026-07-20T07:00:00+00:00",
        "code_inputs": [
            {
                "saved_hash": cell_content_hash("unchanged"),
                "current_content_md": "unchanged",
                "updated_at": "2026-07-20T05:00:00+00:00",
            },
            {
                "saved_hash": cell_content_hash("old"),
                "current_content_md": "![plot](asset://img)\nnew",
                "updated_at": "2026-07-20T09:00:00+00:00",
            },
        ],
    }])

    result = ContentOverviewService(memory, knowhow).get("nb1", "viewer1")

    assert memory.calls == [("viewer1", "nb1", 3)]
    assert knowhow.calls == ["nb1"]
    assert result.memory.total == 4
    assert result.memory.confirmed == 2
    assert result.memory.candidate == 1
    assert [item.id for item in result.memory.recent] == ["m1"]
    assert result.knowhow.table_count == 1
    assert result.knowhow.row_count == 2
    assert result.knowhow.projection_pending == 1
    assert result.knowhow.projection_failed == 1
    assert result.knowhow.stale_code_count == 1
    assert result.knowhow.recent_tables[0].last_activity_at == "2026-07-20T09:00:00+00:00"


def test_content_overview_limits_recent_tables_and_returns_typed_empty_sections():
    rows = [{
        "id": f"t{index}",
        "notebook_id": "nb1",
        "title": f"T{index}",
        "description": "",
        "created_at": f"2026-07-{10 + index:02d}T00:00:00+00:00",
        "updated_at": f"2026-07-{10 + index:02d}T00:00:00+00:00",
        "row_count": index,
        "projection_pending": 0,
        "projection_failed": 0,
        "row_activity_at": "",
        "cell_activity_at": "",
        "code_inputs": [],
    } for index in range(5)]
    empty_memory = FakeMemoryStore({
        "total": 0,
        "confirmed": 0,
        "candidate": 0,
        "recent": [],
    })

    populated = ContentOverviewService(empty_memory, FakeKnowhowStore(rows)).get("nb1", "u1")
    empty = ContentOverviewService(empty_memory, FakeKnowhowStore([])).get("nb1", "u1")

    assert [table.id for table in populated.knowhow.recent_tables] == ["t4", "t3", "t2"]
    assert empty.memory.recent == []
    assert empty.knowhow.table_count == 0
    assert empty.knowhow.recent_tables == []
```

- [ ] **Step 2: Run the service tests and verify RED**

Run:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider -n0 backend/tests/test_content_overview_service.py -q
```

Expected: collection fails because `app.services.content_overview` and the new response models do not exist.

- [ ] **Step 3: Add response models and consumer-specific ports**

Add these Pydantic models near `NotebookAnalytics`, and extend the two existing status models:

```python
class MemoryOverviewItem(BaseModel):
    id: str
    title: str
    status: MemoryStatus
    updated_at: str


class MemoryOverviewSummary(BaseModel):
    total: int = 0
    confirmed: int = 0
    candidate: int = 0
    recent: List[MemoryOverviewItem] = Field(default_factory=list)


class KnowhowOverviewTable(BaseModel):
    id: str
    title: str
    row_count: int = 0
    last_activity_at: str = ""


class KnowhowOverviewSummary(BaseModel):
    table_count: int = 0
    row_count: int = 0
    projection_pending: int = 0
    projection_failed: int = 0
    stale_code_count: int = 0
    recent_tables: List[KnowhowOverviewTable] = Field(default_factory=list)


class NotebookContentOverview(BaseModel):
    memory: MemoryOverviewSummary = Field(default_factory=MemoryOverviewSummary)
    knowhow: KnowhowOverviewSummary = Field(default_factory=KnowhowOverviewSummary)
```

Add the following fields to `KnowhowTableSummary`:

```python
    projection_pending: int = 0
    projection_failed: int = 0
    stale_code_count: int = 0
    last_activity_at: str = ""
```

Append these protocols to `backend/app/repositories/ports.py`:

```python
class ContentOverviewMemoryStorePort(Protocol):
    def notebook_content_overview(
        self, user_id: str, notebook_id: str, limit: int = 3
    ) -> dict[str, Any]: ...


class ContentOverviewKnowhowStorePort(Protocol):
    def knowhow_table_health_inputs(
        self, notebook_id: str
    ) -> list[dict[str, Any]]: ...
```

- [ ] **Step 4: Add the two-query Memory store projection**

Add this method to `MemoryStore`; it counts every lifecycle state in `total`, counts only the requested active states separately, and returns only active recent metadata:

```python
    def notebook_content_overview(
        self, user_id: str, notebook_id: str, limit: int = 3
    ) -> dict[str, Any]:
        bounded_limit = max(1, min(3, int(limit)))
        with self.database.connect() as db:
            counts = db.execute(
                "SELECT COUNT(*) AS total,"
                "COALESCE(SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END),0) AS confirmed,"
                "COALESCE(SUM(CASE WHEN status='candidate' THEN 1 ELSE 0 END),0) AS candidate "
                "FROM memory_items WHERE created_by=? AND notebook_id=?",
                (user_id, notebook_id),
            ).fetchone()
            rows = db.execute(
                "SELECT id,title,status,updated_at FROM memory_items "
                "WHERE created_by=? AND notebook_id=? "
                "AND status IN ('confirmed','candidate') "
                "ORDER BY updated_at DESC,id DESC LIMIT ?",
                (user_id, notebook_id, bounded_limit),
            ).fetchall()
        return {
            "total": int(counts["total"]),
            "confirmed": int(counts["confirmed"]),
            "candidate": int(counts["candidate"]),
            "recent": [dict(row) for row in rows],
        }
```

- [ ] **Step 5: Replace the Knowhow summary read with a four-query health-input projection**

Implement `KnowhowStore.knowhow_table_health_inputs()` with exactly four statement families: table rows; row counts/status/activity; cell activity; code attachment/current-cell pairs. Assemble `code_inputs` in memory and keep SQL out of the service:

```python
    def knowhow_table_health_inputs(self, notebook_id: str) -> list[dict]:
        with self.database.connect() as db:
            tables = db.execute(
                "SELECT id,notebook_id,title,description,created_at,updated_at "
                "FROM knowhow_tables WHERE notebook_id=? ORDER BY created_at,id",
                (notebook_id,),
            ).fetchall()
            table_ids = [row["id"] for row in tables]
            row_stats = {}
            cell_activity = {}
            code_inputs = {table_id: [] for table_id in table_ids}
            if table_ids:
                placeholders = ",".join("?" for _ in table_ids)
                row_stats = {
                    row["table_id"]: dict(row)
                    for row in db.execute(
                        "SELECT table_id,COUNT(*) AS row_count,"
                        "COALESCE(SUM(CASE WHEN projection_status IN ('pending','syncing') "
                        "THEN 1 ELSE 0 END),0) "
                        "AS projection_pending,"
                        "COALESCE(SUM(CASE WHEN projection_status='failed' THEN 1 ELSE 0 END),0) "
                        "AS projection_failed,MAX(updated_at) AS row_activity_at "
                        f"FROM knowhow_rows WHERE table_id IN ({placeholders}) GROUP BY table_id",
                        table_ids,
                    ).fetchall()
                }
                cell_activity = {
                    row["table_id"]: row["cell_activity_at"] or ""
                    for row in db.execute(
                        "SELECT r.table_id,MAX(c.updated_at) AS cell_activity_at "
                        "FROM knowhow_cells c JOIN knowhow_rows r ON r.id=c.row_id "
                        f"WHERE r.table_id IN ({placeholders}) GROUP BY r.table_id",
                        table_ids,
                    ).fetchall()
                }
                for row in db.execute(
                    "SELECT r.table_id,cc.cell_content_hash AS saved_hash,"
                    "COALESCE(c.content_md,'') AS current_content_md,cc.updated_at "
                    "FROM knowhow_cell_code cc "
                    "JOIN knowhow_rows r ON r.id=cc.row_id "
                    "LEFT JOIN knowhow_cells c "
                    "ON c.row_id=cc.row_id AND c.column_id=cc.column_id "
                    f"WHERE r.table_id IN ({placeholders})",
                    table_ids,
                ).fetchall():
                    code_inputs[row["table_id"]].append(dict(row))
        result = []
        for table in tables:
            stats = row_stats.get(table["id"], {})
            result.append({
                **dict(table),
                "row_count": int(stats.get("row_count", 0)),
                "projection_pending": int(stats.get("projection_pending", 0)),
                "projection_failed": int(stats.get("projection_failed", 0)),
                "row_activity_at": stats.get("row_activity_at") or "",
                "cell_activity_at": cell_activity.get(table["id"], ""),
                "code_inputs": code_inputs[table["id"]],
            })
        return result

    def list_knowhow_tables(self, notebook_id: str) -> list[dict]:
        return self.knowhow_table_health_inputs(notebook_id)
```

- [ ] **Step 6: Implement the application service**

Create `backend/app/services/content_overview.py`:

```python
from __future__ import annotations

from app.models.schemas import (
    KnowhowOverviewSummary,
    KnowhowOverviewTable,
    KnowhowTableSummary,
    MemoryOverviewSummary,
    NotebookContentOverview,
)
from app.repositories.ports import (
    ContentOverviewKnowhowStorePort,
    ContentOverviewMemoryStorePort,
)
from app.services.knowhow.api import cell_content_hash


class ContentOverviewService:
    def __init__(
        self,
        memory_store: ContentOverviewMemoryStorePort,
        knowhow_store: ContentOverviewKnowhowStorePort,
    ) -> None:
        self.memory_store = memory_store
        self.knowhow_store = knowhow_store

    def knowhow_tables(self, notebook_id: str) -> list[KnowhowTableSummary]:
        summaries = []
        for row in self.knowhow_store.knowhow_table_health_inputs(notebook_id):
            code_inputs = row["code_inputs"]
            stale_count = sum(
                item["saved_hash"] != cell_content_hash(item["current_content_md"])
                for item in code_inputs
            )
            activity = [
                row["created_at"],
                row["updated_at"],
                row["row_activity_at"],
                row["cell_activity_at"],
                *(item["updated_at"] for item in code_inputs),
            ]
            summaries.append(KnowhowTableSummary(
                id=row["id"],
                notebook_id=row["notebook_id"],
                title=row["title"],
                description=row["description"],
                row_count=row["row_count"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                projection_pending=row["projection_pending"],
                projection_failed=row["projection_failed"],
                stale_code_count=stale_count,
                last_activity_at=max(value for value in activity if value),
            ))
        return summaries

    def get(self, notebook_id: str, user_id: str) -> NotebookContentOverview:
        memory = self.memory_store.notebook_content_overview(
            user_id, notebook_id, limit=3
        )
        tables = self.knowhow_tables(notebook_id)
        recent_tables = sorted(
            tables,
            key=lambda table: (table.last_activity_at, table.id),
            reverse=True,
        )[:3]
        return NotebookContentOverview(
            memory=MemoryOverviewSummary(**memory),
            knowhow=KnowhowOverviewSummary(
                table_count=len(tables),
                row_count=sum(table.row_count for table in tables),
                projection_pending=sum(
                    table.projection_pending for table in tables
                ),
                projection_failed=sum(table.projection_failed for table in tables),
                stale_code_count=sum(table.stale_code_count for table in tables),
                recent_tables=[
                    KnowhowOverviewTable(
                        id=table.id,
                        title=table.title,
                        row_count=table.row_count,
                        last_activity_at=table.last_activity_at,
                    )
                    for table in recent_tables
                ],
            ),
        )
```

- [ ] **Step 7: Pin the fixed query count and store semantics**

Extend `backend/tests/test_knowhow_store.py` with a trace-callback test that creates 25 tables, sets one row to `syncing` and one to `failed`, calls `knowhow_table_health_inputs`, and asserts the SELECT count stays four and the two states contribute to `projection_pending` and `projection_failed` respectively. Add a Memory-store test in `backend/tests/test_content_overview_service.py` using `SQLiteRepository` that inserts confirmed, candidate, rejected, and deprecated rows for two users and two notebooks, then asserts `total=4`, active counts `2/1`, recent active only, and no foreign rows. Trace that Memory method and assert it executes exactly two SELECT statements.

The query-count assertion is:

```python
selects = [
    sql for sql in statements
    if sql.lstrip().upper().startswith("SELECT")
]
assert len(selects) == 4
assert len(result) == 25
assert sum(row["projection_pending"] for row in result) == 1
assert sum(row["projection_failed"] for row in result) == 1
```

The Memory query-count assertion is:

```python
memory_selects = [
    sql for sql in statements
    if sql.lstrip().upper().startswith("SELECT")
    and "memory_items" in sql
]
assert len(memory_selects) == 2
```

- [ ] **Step 8: Run focused tests and commit**

Run:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider -n0 backend/tests/test_content_overview_service.py backend/tests/test_knowhow_store.py -q
```

Expected: all selected tests pass.

Commit:

```bash
git add backend/app/models/schemas.py backend/app/repositories/ports.py backend/app/repositories/sqlite/memory_store.py backend/app/repositories/sqlite/knowhow_store.py backend/app/services/content_overview.py backend/tests/test_content_overview_service.py backend/tests/test_knowhow_store.py
git commit -m "feat: add content overview projections"
```

---

### Task 2: Expose the viewer-aware endpoint and unify the existing Knowhow table list

**Files:**
- Create: `backend/app/api/content_overview_routes.py`
- Create: `backend/tests/test_content_overview_api.py`
- Modify: `backend/app/api/deps.py`
- Modify: `backend/app/api/routes.py`
- Modify: `backend/tests/test_knowhow_api.py`

**Interfaces:**
- Consumes: `ContentOverviewService.get()` and `ContentOverviewService.knowhow_tables()` from Task 1.
- Produces: `GET /api/notebooks/{notebook_id}/analytics/content-overview`.
- Produces: additive health fields on `GET /api/notebooks/{notebook_id}/knowhow`.

- [ ] **Step 1: Write failing API tests**

Create `backend/tests/test_content_overview_api.py` using `TestClient(create_app())` and the existing register/login helpers. Pin these behaviors with separate test functions:

```python
def test_content_overview_isolates_memory_but_shares_notebook_knowhow():
    owner_body = client.get(
        f"/api/notebooks/{notebook_id}/analytics/content-overview",
        headers=owner_headers,
    ).json()
    reader_body = client.get(
        f"/api/notebooks/{notebook_id}/analytics/content-overview",
        headers=reader_headers,
    ).json()
    assert owner_body["memory"]["total"] == 2
    assert reader_body["memory"]["total"] == 1
    assert owner_body["knowhow"] == reader_body["knowhow"]


def test_content_overview_returns_zero_sections_for_empty_notebook():
    response = client.get(
        f"/api/notebooks/{empty_notebook_id}/analytics/content-overview",
        headers=owner_headers,
    )
    assert response.status_code == 200
    assert response.json() == {
        "memory": {
            "total": 0,
            "confirmed": 0,
            "candidate": 0,
            "recent": [],
        },
        "knowhow": {
            "table_count": 0,
            "row_count": 0,
            "projection_pending": 0,
            "projection_failed": 0,
            "stale_code_count": 0,
            "recent_tables": [],
        },
    }


def test_content_overview_uses_uniform_not_found_for_stranger_and_missing():
    stranger = client.get(
        f"/api/notebooks/{notebook_id}/analytics/content-overview",
        headers=stranger_headers,
    )
    missing = client.get(
        "/api/notebooks/missing/analytics/content-overview",
        headers=owner_headers,
    )
    assert stranger.status_code == 404
    assert missing.status_code == 404
    assert stranger.json() == missing.json()


def test_admin_sees_only_admin_owned_memory_in_an_accessible_notebook():
    body = client.get(
        f"/api/notebooks/{admin_notebook_id}/analytics/content-overview",
        headers=admin_headers,
    ).json()
    assert body["memory"]["total"] == 1
    assert [item["id"] for item in body["memory"]["recent"]] == [admin_memory_id]
```

Also extend the Knowhow API list test to assert the response includes:

```python
assert body[0]["projection_pending"] == 1
assert body[0]["projection_failed"] == 0
assert body[0]["stale_code_count"] == 0
assert body[0]["last_activity_at"]
```

- [ ] **Step 2: Run API tests and verify RED**

Run:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider -n0 backend/tests/test_content_overview_api.py backend/tests/test_knowhow_api.py -q
```

Expected: the new endpoint returns 404 and the Knowhow summary lacks the additive health fields.

- [ ] **Step 3: Add dependency composition at the end of `deps.py`**

Append:

```python
from app.services.content_overview import ContentOverviewService  # noqa: E402


def content_overview_service() -> ContentOverviewService:
    runtime = repository()._runtime  # type: ignore[attr-defined]
    return ContentOverviewService(runtime.memory_store, runtime.knowhow_store)
```

Do not cache this service separately; tests already clear the repository singleton and must not retain stores from an earlier database.

- [ ] **Step 4: Add the focused route module**

Create `backend/app/api/content_overview_routes.py`:

```python
from fastapi import APIRouter, Depends

from app.api.deps import (
    content_overview_service,
    get_current_user,
    require_notebook_read,
)
from app.models.schemas import NotebookContentOverview, UserProfile


router = APIRouter()


@router.get(
    "/notebooks/{notebook_id}/analytics/content-overview",
    response_model=NotebookContentOverview,
    dependencies=[Depends(require_notebook_read)],
)
def notebook_content_overview(
    notebook_id: str,
    user: UserProfile = Depends(get_current_user),
) -> NotebookContentOverview:
    return content_overview_service().get(notebook_id, user.id)
```

- [ ] **Step 5: Register the route without shifting pinned sites and reuse the service for Knowhow summaries**

Replace only the return expression in the existing Knowhow list route:

```python
def list_knowhow_tables(notebook_id: str) -> List[dict]:
    return content_overview_service().knowhow_tables(notebook_id)
```

Append this registration at the end of `backend/app/api/routes.py`:

```python
from app.api.content_overview_routes import router as content_overview_router  # noqa: E402
from app.api.deps import content_overview_service  # noqa: E402

router.include_router(content_overview_router)
```

- [ ] **Step 6: Run API and architecture tests**

Run:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider -n0 backend/tests/test_content_overview_api.py backend/tests/test_knowhow_api.py backend/tests/test_repository_callers_static.py backend/tests/test_repository_surface_manifest.py backend/tests/test_repository_dependency_contract.py -q
```

Expected: endpoint, Knowhow list, privacy, and architecture tests all pass.

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/content_overview_routes.py backend/app/api/deps.py backend/app/api/routes.py backend/tests/test_content_overview_api.py backend/tests/test_knowhow_api.py
git commit -m "feat: expose viewer-aware content analytics"
```

---

### Task 3: Separate visible source counts from physical derived-content accounting

**Files:**
- Modify: `backend/app/models/schemas.py`
- Modify: `backend/app/repositories/sqlite/knowledge_counts_cache.py`
- Modify: `backend/app/repositories/sqlite/query_store.py`
- Modify: `backend/app/repositories/sqlite/index_projection_store.py`
- Modify: `backend/app/services/notebook_catalog.py`
- Modify: `backend/app/services/notebook_sharing.py`
- Modify: `backend/app/services/scale_artifact_runtime.py`
- Modify: `backend/tests/test_knowledge_counts_cache.py`
- Modify: `backend/tests/test_notebook_summary_query.py`
- Modify: `backend/tests/test_notebook_share_copy.py`
- Modify: `backend/tests/test_scale_delta_policy.py`
- Modify: `backend/tests/test_index_build_consolidation.py`
- Modify: `frontend/app/scale-index.ts`
- Modify: `frontend/app/scale-index.test.mjs`
- Modify: `frontend/app/kg-build-status.ts`
- Modify: `frontend/app/kg-build-status.test.mjs`

**Interfaces:**
- Produces: `knowledge_counts_cache.visible_pending_source_count(db, notebook_id) -> int`.
- Produces: `QueryStore.visible_pending_kg_source_count(db, notebook_id) -> int`.
- Produces: `IndexProjectionStore.visible_source_ids(notebook_id, source_ids) -> list[str]`.
- Produces: `ScaleIndexStatus.has_unindexed_content: bool`.
- Keeps: physical `pending_source_count`, `notebook_copy_stats()["size"]["sources"]`, `_index_delta()["delta_sources"]`, and scheduler counters unchanged.

- [ ] **Step 1: Write failing source-semantics tests**

Add fixtures containing one uploaded source, one Memory source, and one Knowhow source. Pin all five surfaces:

```python
assert summary.counts["sources"] == 1
assert summary.kg_pending_sources == 1
assert preview["source_count"] == 1
assert preview["size"]["sources"] == 3
assert scale_status["unindexed_sources"] == 1
assert scale_status["has_unindexed_content"] is True
```

Add the derived-only scale case:

```python
assert scale_status["unindexed_sources"] == 0
assert scale_status["has_unindexed_content"] is True
assert describe_expected_primary_operation == "update"
```

In `test_knowledge_counts_cache.py`, assert physical and visible pending caches diverge correctly and both are invalidated:

```python
assert pending_source_count(db, notebook_id) == 3
assert visible_pending_source_count(db, notebook_id) == 1
invalidate(notebook_id)
assert visible_pending_source_count(db, notebook_id) == 1
```

- [ ] **Step 2: Run source-count tests and verify RED**

Run:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider -n0 backend/tests/test_knowledge_counts_cache.py backend/tests/test_notebook_summary_query.py backend/tests/test_notebook_share_copy.py backend/tests/test_scale_delta_policy.py backend/tests/test_index_build_consolidation.py -q
```

Expected: user-visible pending/share/scale assertions expose physical derived rows, and `has_unindexed_content` is absent.

- [ ] **Step 3: Add a sibling visible pending-source cache**

In `knowledge_counts_cache.py`, add `_VISIBLE_PENDING` beside `_PENDING`. Extract the shared query into a private helper whose `visible_only` branch adds the source-type predicate:

```python
def _pending_source_count_query(
    db: sqlite3.Connection, notebook_id: str, *, visible_only: bool
) -> int:
    visible_clause = (
        "AND s.source_type NOT IN ('memory','knowhow') " if visible_only else ""
    )
    row = db.execute(
        "SELECT COUNT(*) FROM sources s WHERE s.notebook_id = ? "
        + visible_clause
        + "AND EXISTS (SELECT 1 FROM source_elements e WHERE e.source_id = s.id) "
        "AND NOT EXISTS (SELECT 1 FROM knowledge_objects k "
        "WHERE k.source_id = s.id AND k.source_id != '' "
        "AND COALESCE(("
        "SELECT er.status FROM extraction_runs er "
        "WHERE er.source_id=s.id AND er.run_type='kg' "
        "ORDER BY er.created_at DESC,er.rowid DESC LIMIT 1"
        "),'completed')='completed')",
        (notebook_id,),
    ).fetchone()
    return int(row[0])
```

Keep `pending_source_count()` on `_PENDING` with `visible_only=False`. Add:

```python
def visible_pending_source_count(
    db: sqlite3.Connection, notebook_id: str
) -> int:
    seq = _mutation_seq(db, notebook_id)
    with _LOCK:
        hit = _VISIBLE_PENDING.get(notebook_id)
        if hit is not None and hit[0] == seq:
            _VISIBLE_PENDING.move_to_end(notebook_id)
            return hit[1]
    count = _pending_source_count_query(db, notebook_id, visible_only=True)
    with _LOCK:
        _VISIBLE_PENDING[notebook_id] = (seq, count)
        _VISIBLE_PENDING.move_to_end(notebook_id)
        while len(_VISIBLE_PENDING) > _MAX_NOTEBOOKS:
            _VISIBLE_PENDING.popitem(last=False)
    return count
```

Warm this cache in `warm_all()` and clear it in both branches of `invalidate()`.

- [ ] **Step 4: Route notebook summaries and share preview to visible counts**

Add:

```python
    @staticmethod
    def visible_pending_kg_source_count(
        db: sqlite3.Connection, notebook_id: str
    ) -> int:
        from app.repositories.sqlite import knowledge_counts_cache
        return knowledge_counts_cache.visible_pending_source_count(db, notebook_id)
```

Change `NotebookSummaryQuery.count_pending_kg_sources()` to call this method. In `NotebookSharingService.shared_preview()`, keep `stats["size"]` unchanged and replace the display count with:

```python
            "source_count": int(notebook.counts.get("sources", 0)),
```

- [ ] **Step 5: Add visible delta filtering without changing the physical index delta**

Add this method to `IndexProjectionStore` using its injected batching helper so SQLite variable limits remain bounded:

```python
    def visible_source_ids(
        self, notebook_id: str, source_ids: List[str]
    ) -> List[str]:
        if not source_ids:
            return []
        visible = set()
        with self.connect() as db:
            for batch in self.in_batches(source_ids):
                placeholders = ",".join("?" for _ in batch)
                rows = db.execute(
                    "SELECT id FROM sources WHERE notebook_id=? "
                    "AND source_type NOT IN ('memory','knowhow') "
                    f"AND id IN ({placeholders})",
                    (notebook_id, *batch),
                ).fetchall()
                visible.update(row["id"] for row in rows)
        return [source_id for source_id in source_ids if source_id in visible]
```

In `ScaleArtifactRuntime.status()`:

```python
        delta_sources = list(delta["delta_sources"])
        result = {
            "exists": exists,
            "building": building,
            "eligible": eligible,
            "delta_chunks": int(delta["delta_chunks"]),
            "total_chunks": int(total_chunks),
            "unindexed_sources": len(
                self.projections.visible_source_ids(notebook_id, delta_sources)
            ),
            "has_unindexed_content": bool(delta_sources),
            "delta_searchable": bool(self.settings.scale_search_include_delta),
        }
```

Add `has_unindexed_content: bool = False` to the Pydantic `ScaleIndexStatus`. Do not alter `_index_delta`, fold thresholds, builder inputs, or physical copy statistics.

- [ ] **Step 6: Update frontend scale action semantics and KG progress wording**

Add `has_unindexed_content?: boolean` to the TypeScript `ScaleIndexStatus`. In `describeScaleIndex`, derive:

```typescript
const hasUnindexedContent = s.has_unindexed_content ?? (s.unindexed_sources ?? 0) > 0;
```

Use `hasUnindexedContent` for the stale `update` versus `rebuild` operation. Keep `unindexed_sources` only for the visible count in labels. In `scaleIndexOpConfirm`, render `新增/变更内容` when the operation is update and the visible count is zero.

Change mixed physical KG job wording in `kg-build-status.ts`:

```typescript
label: `正在分析 ${job.completed_sources}/${job.total_sources} 项内容`
detail: job.failed_sources > 0
  ? `${job.failed_sources} 项内容未完成；其余内容继续处理`
  : "正在提取知识对象与关系"
```

Use `项内容未完成` in the completed/failure variants as well. Update exact strings in `kg-build-status.test.mjs`, and add a scale-index test where `unindexed_sources=0` and `has_unindexed_content=true` still chooses update.

- [ ] **Step 7: Run focused backend and frontend tests**

Run:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider -n0 backend/tests/test_knowledge_counts_cache.py backend/tests/test_notebook_summary_query.py backend/tests/test_notebook_share_copy.py backend/tests/test_scale_delta_policy.py backend/tests/test_index_build_consolidation.py -q
cd frontend && node --test app/scale-index.test.mjs app/kg-build-status.test.mjs
```

Expected: all selected tests pass and no physical accounting assertion changes.

- [ ] **Step 8: Commit**

```bash
git add backend/app/models/schemas.py backend/app/repositories/sqlite/knowledge_counts_cache.py backend/app/repositories/sqlite/query_store.py backend/app/repositories/sqlite/index_projection_store.py backend/app/services/notebook_catalog.py backend/app/services/notebook_sharing.py backend/app/services/scale_artifact_runtime.py backend/tests/test_knowledge_counts_cache.py backend/tests/test_notebook_summary_query.py backend/tests/test_notebook_share_copy.py backend/tests/test_scale_delta_policy.py backend/tests/test_index_build_consolidation.py frontend/app/scale-index.ts frontend/app/scale-index.test.mjs frontend/app/kg-build-status.ts frontend/app/kg-build-status.test.mjs
git commit -m "fix: separate visible and derived source counts"
```

---

### Task 4: Add typed Memory deep links and Knowhow health filters

**Files:**
- Modify: `frontend/app/memory-model.ts`
- Modify: `frontend/app/memory-navigation.test.mjs`
- Modify: `frontend/app/memory-panel.tsx`
- Modify: `frontend/app/knowhow-model.ts`
- Modify: `frontend/app/knowhow-model.test.mjs`
- Modify: `frontend/app/knowhow-panel.tsx`

**Interfaces:**
- Produces: `MemoryNavigationTarget` and extended `memoryHash(scopeNotebookId, target)` / `parseMemoryHash(hash)`.
- Produces: `KnowhowHealthFilter = "all" | "projection_pending" | "projection_failed" | "stale_code"`.
- Produces: `filterKnowhowTables(tables, filter)`.
- Produces: optional Memory panel props `initialNotebookId`, `initialStatus`, `initialMemoryId`.
- Produces: optional Knowhow panel prop `initialHealthFilter`.

- [ ] **Step 1: Write failing pure navigation and filter tests**

Add these assertions:

```javascript
assert.equal(
  memoryHash(null, {
    notebookId: "nb 1",
    status: "confirmed",
    itemId: "mem/1",
  }),
  "#memory&notebook=nb%201&status=confirmed&item=mem%2F1",
);
assert.deepEqual(
  parseMemoryHash("#memory&notebook=nb%201&status=confirmed&item=mem%2F1"),
  {
    scope: "global",
    notebookId: null,
    filterNotebookId: "nb 1",
    status: "confirmed",
    itemId: "mem/1",
  },
);
assert.equal(memoryHash("nb-1"), "#notebook=nb-1&tab=memory");
```

In `knowhow-model.test.mjs`, add:

```javascript
assert.deepEqual(
  filterKnowhowTables(tables, "projection_pending").map((table) => table.id),
  ["pending"],
);
assert.deepEqual(
  filterKnowhowTables(tables, "projection_failed").map((table) => table.id),
  ["failed"],
);
assert.deepEqual(
  filterKnowhowTables(tables, "stale_code").map((table) => table.id),
  ["stale"],
);
```

- [ ] **Step 2: Run pure frontend tests and verify RED**

Run:

```bash
cd frontend && node --test app/memory-navigation.test.mjs app/knowhow-model.test.mjs
```

Expected: extended hash arguments, parsed fields, health fields, and filter helper are absent.

- [ ] **Step 3: Extend the Memory hash contract without breaking existing hashes**

Add:

```typescript
export type MemoryNavigationTarget = {
  notebookId?: string | null;
  status?: MemoryStatus | null;
  itemId?: string | null;
};

export type ParsedMemoryHash = {
  scope: MemoryScope;
  notebookId: string | null;
  filterNotebookId: string | null;
  status: MemoryStatus | null;
  itemId: string | null;
};
```

Implement:

```typescript
export function memoryHash(
  notebookId: string | null,
  target: MemoryNavigationTarget = {},
): string {
  if (notebookId) {
    return `#notebook=${encodeURIComponent(notebookId)}&tab=memory`;
  }
  const parts = ["memory"];
  if (target.notebookId) parts.push(`notebook=${encodeURIComponent(target.notebookId)}`);
  if (target.status) parts.push(`status=${encodeURIComponent(target.status)}`);
  if (target.itemId) parts.push(`item=${encodeURIComponent(target.itemId)}`);
  return `#${parts.join("&")}`;
}

export function parseMemoryHash(hash: string): ParsedMemoryHash | null {
  const raw = hash.replace(/^#/, "");
  if (raw === "memory" || raw.startsWith("memory&")) {
    const params = new URLSearchParams(raw === "memory" ? "" : raw.slice(7));
    const status = params.get("status");
    const validStatus = status === "candidate"
      || status === "confirmed"
      || status === "rejected"
      || status === "deprecated";
    return {
      scope: "global",
      notebookId: null,
      filterNotebookId: params.get("notebook"),
      status: validStatus ? status : null,
      itemId: params.get("item"),
    };
  }
  const params = new URLSearchParams(raw);
  const notebookId = params.get("notebook");
  if (notebookId && params.get("tab") === "memory") {
    return {
      scope: "notebook",
      notebookId,
      filterNotebookId: null,
      status: null,
      itemId: null,
    };
  }
  return null;
}
```

Update existing exact-object assertions to include the three nullable target fields. Keep
the Memory and workspace parsers mutually exclusive by starting
`parseWorkspaceHash()` with:

```typescript
const raw = hash.replace(/^#/, "");
if (raw === "memory" || raw.startsWith("memory&")) return null;
const params = new URLSearchParams(raw);
```

Add the new global Memory deep-link hash to the existing mutual-exclusion test.

- [ ] **Step 4: Restore Memory filters and existing detail expansion from props**

Extend `MemoryPanel` props with:

```typescript
  initialNotebookId?: string | null;
  initialStatus?: MemoryStatus | null;
  initialMemoryId?: string | null;
```

Initialize global filter state from those props:

```typescript
const [notebookFilter, setNotebookFilter] = useState(
  scope === "global" ? initialNotebookId ?? "" : "",
);
const [status, setStatus] = useState<MemoryStatus | "all">(
  initialStatus ?? "all",
);
```

Add a prop-change effect that resets pagination before applying a new navigation target:

```typescript
useEffect(() => {
  if (scope !== "global") return;
  setNotebookFilter(initialNotebookId ?? "");
  setStatus(initialStatus ?? "all");
  setPage(0);
}, [scope, initialNotebookId, initialStatus]);
```

Expand the existing detail card when the targeted item is present in the loaded
page:

```typescript
useEffect(() => {
  if (!initialMemoryId || !items.some((item) => item.id === initialMemoryId)) return;
  setExpandedIds((previous) => {
    if (previous.has(initialMemoryId)) return previous;
    return new Set(previous).add(initialMemoryId);
  });
}, [initialMemoryId, items]);
```

Do not set `editingId`; navigation opens read detail, not edit mode.

- [ ] **Step 5: Map and filter Knowhow summary health**

Extend both wire and domain table summary types:

```typescript
export type KnowhowTableSummary = {
  id: string;
  title: string;
  description: string;
  rowCount: number;
  projectionPending: number;
  projectionFailed: number;
  staleCodeCount: number;
  lastActivityAt: string;
};

export type KnowhowHealthFilter =
  | "all"
  | "projection_pending"
  | "projection_failed"
  | "stale_code";
```

Map snake-case fields with zero/empty defaults, then add:

```typescript
export function filterKnowhowTables(
  tables: KnowhowTableSummary[],
  filter: KnowhowHealthFilter,
): KnowhowTableSummary[] {
  if (filter === "projection_pending") {
    return tables.filter((table) => table.projectionPending > 0);
  }
  if (filter === "projection_failed") {
    return tables.filter((table) => table.projectionFailed > 0);
  }
  if (filter === "stale_code") {
    return tables.filter((table) => table.staleCodeCount > 0);
  }
  return tables;
}
```

- [ ] **Step 6: Render table-level health filters in the existing Knowhow list**

Add `initialHealthFilter?: KnowhowHealthFilter` to `KnowhowPanelProps`, initialize `healthFilter`, derive `visibleTables = filterKnowhowTables(tables ?? [], healthFilter)`, and pass `visibleTables` into `KnowhowTableList`.

Render a labeled select in the existing toolbar:

```tsx
<label className="knowhow-health-filter">
  <span>状态</span>
  <select
    value={healthFilter}
    onChange={(event) => setHealthFilter(event.target.value as KnowhowHealthFilter)}
  >
    <option value="all">全部表</option>
    <option value="projection_pending">待投影</option>
    <option value="projection_failed">投影失败</option>
    <option value="stale_code">代码已过期</option>
  </select>
</label>
```

When the filtered list is empty but the unfiltered list is not, render `当前筛选下没有表` and keep the selector available. Each result remains the existing table card and opens the existing table editor.

- [ ] **Step 7: Run pure tests and TypeScript**

Run:

```bash
cd frontend && node --test app/memory-navigation.test.mjs app/knowhow-model.test.mjs
cd frontend && npx tsc --noEmit
```

Expected: both Node suites and TypeScript pass.

- [ ] **Step 8: Commit**

```bash
git add frontend/app/memory-model.ts frontend/app/memory-navigation.test.mjs frontend/app/memory-panel.tsx frontend/app/knowhow-model.ts frontend/app/knowhow-model.test.mjs frontend/app/knowhow-panel.tsx
git commit -m "feat: add content asset navigation targets"
```

---

### Task 5: Build the render-only content overview cards

**Files:**
- Create: `frontend/app/content-overview-cards.tsx`
- Create: `frontend/app/content-overview-cards.component.test.tsx`
- Modify: `frontend/app/workspace-model.ts`
- Modify: `frontend/app/globals.css`

**Interfaces:**
- Produces: `NotebookContentOverview` frontend wire type.
- Produces: `ContentOverviewCards` callbacks for Memory status/item and Knowhow filter/table navigation.
- Does not fetch data or own destination state.

- [ ] **Step 1: Write failing component tests for loading, normal, empty, failure, navigation, and read-only display**

Use React Testing Library to render the component with a complete overview fixture. Assert:

```tsx
expect(screen.getByRole("heading", { name: "Memory" })).toBeInTheDocument();
expect(screen.getByText("4 条")).toBeInTheDocument();
fireEvent.click(screen.getByRole("button", { name: "查看 2 条已确认记忆" }));
expect(onOpenMemory).toHaveBeenCalledWith("confirmed", null);
fireEvent.click(screen.getByRole("button", { name: "打开记忆 Stable memory" }));
expect(onOpenMemory).toHaveBeenCalledWith(null, "m1");
fireEvent.click(screen.getByRole("button", { name: "查看 1 张待投影表" }));
expect(onOpenKnowhow).toHaveBeenCalledWith("projection_pending", null);
fireEvent.click(screen.getByRole("button", { name: "打开 Knowhow 表 Bring-up" }));
expect(onOpenKnowhow).toHaveBeenCalledWith("all", "t1");
```

Add separate renders asserting `正在加载内容资产…`, `内容资产暂时不可用`, and zero-state copy. Pass `readOnly` and assert navigation buttons remain enabled while no edit control is rendered.

- [ ] **Step 2: Run the component test and verify RED**

Run:

```bash
cd frontend && npx vitest run app/content-overview-cards.component.test.tsx
```

Expected: the component module and overview type do not exist.

- [ ] **Step 3: Add frontend response types**

Add to `workspace-model.ts`:

```typescript
export type MemoryOverviewItem = {
  id: string;
  title: string;
  status: "candidate" | "confirmed";
  updated_at: string;
};

export type KnowhowOverviewTable = {
  id: string;
  title: string;
  row_count: number;
  last_activity_at: string;
};

export type NotebookContentOverview = {
  memory: {
    total: number;
    confirmed: number;
    candidate: number;
    recent: MemoryOverviewItem[];
  };
  knowhow: {
    table_count: number;
    row_count: number;
    projection_pending: number;
    projection_failed: number;
    stale_code_count: number;
    recent_tables: KnowhowOverviewTable[];
  };
};
```

- [ ] **Step 4: Implement the render-only component**

Create a component with this public contract:

```typescript
export type ContentOverviewCardsProps = {
  overview: NotebookContentOverview | null;
  loading: boolean;
  error: string;
  readOnly: boolean;
  onOpenMemory: (
    status: "candidate" | "confirmed" | null,
    itemId: string | null,
  ) => void;
  onOpenKnowhow: (
    filter: KnowhowHealthFilter,
    tableId: string | null,
  ) => void;
};
```

The component renders a section labeled `内容资产`, two articles with headings `Memory` and `Knowhow`, metric buttons with the exact accessible names from Step 1, at most three recent entries from each payload, and `查看全部` buttons. Loading and error states occupy only this section. `readOnly` adds a `只读` badge to Knowhow and never disables navigation.

- [ ] **Step 5: Add responsive styles**

Add dedicated classes:

```css
.content-overview-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
}

.content-overview-card {
  min-width: 0;
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: 14px;
  background: var(--panel);
}

.content-overview-recent button {
  width: 100%;
  min-width: 0;
  text-align: left;
}

@media (max-width: 760px) {
  .content-overview-grid {
    grid-template-columns: 1fr;
  }
}
```

Use existing color variables found in `globals.css`; do not introduce a fixed palette that conflicts with the current theme.

- [ ] **Step 6: Run component tests and TypeScript**

Run:

```bash
cd frontend && npx vitest run app/content-overview-cards.component.test.tsx
cd frontend && npx tsc --noEmit
```

Expected: component tests and TypeScript pass.

- [ ] **Step 7: Commit**

```bash
git add frontend/app/content-overview-cards.tsx frontend/app/content-overview-cards.component.test.tsx frontend/app/workspace-model.ts frontend/app/globals.css
git commit -m "feat: add analytics content asset cards"
```

---

### Task 6: Wire parallel loading and destination navigation into the existing page

**Files:**
- Create: `frontend/app/analytics-loaders.ts`
- Create: `frontend/app/analytics-loaders.test.mjs`
- Modify: `frontend/app/page.tsx`
- Modify: `frontend/app/memory-navigation.test.mjs`

**Interfaces:**
- Consumes: `ContentOverviewCards`, `NotebookContentOverview`, extended Memory hash/panel props, and Knowhow health-filter prop.
- Produces: `startAnalyticsLoads()` which starts all three reads synchronously and settles the two optional reads independently.
- Keeps: existing `/analytics` and `/index-status` behavior when the new request fails.

- [ ] **Step 1: Write a failing behavioral test for parallel start and optional-request isolation**

Create `frontend/app/analytics-loaders.test.mjs`:

```javascript
import assert from "node:assert/strict";
import test from "node:test";

import { startAnalyticsLoads } from "./analytics-loaders.ts";


test("starts all reads immediately and isolates optional overview failure", async () => {
  const started = [];
  const loads = startAnalyticsLoads({
    analytics: () => {
      started.push("analytics");
      return Promise.resolve({ answers_total: 2 });
    },
    indexStatus: () => {
      started.push("index");
      return Promise.resolve({ kg: { building: false } });
    },
    contentOverview: () => {
      started.push("overview");
      return Promise.reject(new Error("overview unavailable"));
    },
  });

  assert.deepEqual(started, ["analytics", "index", "overview"]);
  assert.deepEqual(await loads.analytics, { answers_total: 2 });
  assert.deepEqual(await loads.indexStatus, {
    ok: true,
    value: { kg: { building: false } },
  });
  assert.deepEqual(await loads.contentOverview, { ok: false });
});
```

- [ ] **Step 2: Run loader tests and verify RED**

Run:

```bash
cd frontend && node --test app/analytics-loaders.test.mjs app/memory-navigation.test.mjs
```

Expected: `analytics-loaders.ts` does not exist.

- [ ] **Step 3: Implement the loader boundary**

Create `frontend/app/analytics-loaders.ts`:

```typescript
export type OptionalLoad<T> =
  | { ok: true; value: T }
  | { ok: false };

async function settleOptional<T>(promise: Promise<T>): Promise<OptionalLoad<T>> {
  try {
    return { ok: true, value: await promise };
  } catch {
    return { ok: false };
  }
}

export function startAnalyticsLoads<A, I, O>(loaders: {
  analytics: () => Promise<A>;
  indexStatus: () => Promise<I>;
  contentOverview: () => Promise<O>;
}): {
  analytics: Promise<A>;
  indexStatus: Promise<OptionalLoad<I>>;
  contentOverview: Promise<OptionalLoad<O>>;
} {
  const analytics = loaders.analytics();
  const indexStatus = loaders.indexStatus();
  const contentOverview = loaders.contentOverview();
  return {
    analytics,
    indexStatus: settleOptional(indexStatus),
    contentOverview: settleOptional(contentOverview),
  };
}
```

- [ ] **Step 4: Add page-owned state and parallel loading**

Add:

```typescript
const [contentOverview, setContentOverview] = useState<NotebookContentOverview | null>(null);
const [contentOverviewLoading, setContentOverviewLoading] = useState(false);
const [contentOverviewError, setContentOverviewError] = useState("");
const [memoryNavigationTarget, setMemoryNavigationTarget] = useState<MemoryNavigationTarget>({});
const [knowhowHealthFilter, setKnowhowHealthFilter] = useState<KnowhowHealthFilter>("all");
```

Change `openAnalytics()` so all requests start together:

```typescript
async function openAnalytics() {
  if (!currentNotebookId) return;
  const nb = currentNotebookId;
  setContentOverviewLoading(true);
  setContentOverviewError("");
  const loads = startAnalyticsLoads({
    analytics: () => api<NotebookAnalytics>(`/notebooks/${nb}/analytics`),
    indexStatus: () => fetchIndexStatus(nb),
    contentOverview: () => api<NotebookContentOverview>(
      `/notebooks/${nb}/analytics/content-overview`,
    ),
  });
  void loads.indexStatus.then((result) => {
    if (!result.ok) return;
    const status = result.value;
    setIndexStatus(status);
    setScaleIndexStatus(status.scale_index);
    if (status.kg.job?.status === "running") setTrackedKgJobId(status.kg.job.job_id);
    if (status.kg.building) setBuildingKg(true);
    if (shouldResumeScaleIndex(status.scale_index)) setBuildingScaleIndex(true);
  });
  void loads.contentOverview.then((result) => {
    if (result.ok) {
      setContentOverview(result.value);
      setContentOverviewError("");
    } else {
      setContentOverview(null);
      setContentOverviewError("内容资产暂时不可用");
    }
    setContentOverviewLoading(false);
  });
  const response = await loads.analytics;
  setAnalytics(response);
}
```

The analytics request retains its existing error path. The overview catch must not clear `analytics` or `indexStatus`.

- [ ] **Step 5: Add navigation callbacks that open existing destinations**

Add:

```typescript
function openAnalyticsMemory(
  status: "candidate" | "confirmed" | null,
  itemId: string | null,
) {
  if (!currentNotebookId) return;
  const target = { notebookId: currentNotebookId, status, itemId };
  setAnalytics(null);
  showGlobalMemory(target);
}

function openAnalyticsKnowhow(
  filter: KnowhowHealthFilter,
  tableId: string | null,
) {
  setKnowhowHealthFilter(filter);
  setKnowhowJumpTarget(tableId ? { tableId, rowId: null } : null);
  setAnalytics(null);
  setKnowhowOpen(true);
}
```

Extend the existing `showGlobalMemory` so ordinary navigation clears an old
analytics filter while deep-link navigation restores an explicit target:

```typescript
function showGlobalMemory(target: MemoryNavigationTarget = {}) {
  showCollection();
  setMemoryNavigationTarget(target);
  setOuterView("memory");
  window.history.replaceState(null, "", memoryHash(null, target));
}
```

When parsing an initial/global Memory hash or handling browser history, call:

```typescript
showGlobalMemory({
  notebookId: memory.filterNotebookId,
  status: memory.status,
  itemId: memory.itemId,
});
```

The existing toolbar call `showGlobalMemory()` therefore resets to the unfiltered
global Memory page, while analytics and browser history restore the requested
notebook/status/item target.

- [ ] **Step 6: Mount cards and pass destination props**

Mount:

```tsx
<ContentOverviewCards
  overview={contentOverview}
  loading={contentOverviewLoading}
  error={contentOverviewError}
  readOnly={isReader}
  onOpenMemory={openAnalyticsMemory}
  onOpenKnowhow={openAnalyticsKnowhow}
/>
```

Place it in the existing analytics modal after the existing notebook metrics and before the index/build-status section.

Pass:

```tsx
<MemoryPanel
  scope="global"
  notebookId={null}
  notebookBases={notebookBasesById}
  sessionSignal={memorySessionAbortRef.current.signal}
  initialNotebookId={memoryNavigationTarget.notebookId}
  initialStatus={memoryNavigationTarget.status}
  initialMemoryId={memoryNavigationTarget.itemId}
/>
```

Pass `initialHealthFilter={knowhowHealthFilter}` to the existing `KnowhowPanel`. Clear the health filter and jump target when the panel closes.

- [ ] **Step 7: Run loader, navigation, component, TypeScript, and production build checks**

Run:

```bash
cd frontend && node --test app/analytics-loaders.test.mjs app/memory-navigation.test.mjs app/knowhow-model.test.mjs
cd frontend && npx vitest run app/content-overview-cards.component.test.tsx
cd frontend && npx tsc --noEmit
cd frontend && npm run build
```

Expected: all tests, type checking, and the production build pass.

- [ ] **Step 8: Commit**

```bash
git add frontend/app/analytics-loaders.ts frontend/app/analytics-loaders.test.mjs frontend/app/page.tsx frontend/app/memory-navigation.test.mjs
git commit -m "feat: connect content assets to existing pages"
```

---

### Task 7: Synchronize documentation and run the complete evidence gate

**Files:**
- Modify: `README.md`
- Modify: `README_zh.md`
- Modify: `AGENTS.md`
- Modify: `fangan_done.md`

**Interfaces:**
- Documents: endpoint, privacy boundary, Knowhow metrics, navigation reuse, visible-source semantics, and `has_unindexed_content`.
- Verifies: one coherent PR, warm local gate ≤60 seconds, Ubuntu Actions green, exact-head subagent review.

- [ ] **Step 1: Update all required documentation together**

Add the following facts in both READMEs and the matching product/architecture constraints in `AGENTS.md`:

```text
The existing notebook analytics view includes separate Memory and Knowhow content-asset cards. Memory metrics are scoped to the current user and notebook; Knowhow metrics follow notebook read access. Cards navigate to the existing Memory and Knowhow pages and never duplicate their editors.
```

Document `GET /notebooks/{id}/analytics/content-overview` and its response fields. State that user-facing source counts exclude hidden Memory/Knowhow projection sources, while `size.sources`, copy thresholds, and background scheduling retain physical accounting. Document that `has_unindexed_content` preserves the scale-index update decision when the visible imported-source delta is zero.

Update `fangan_done.md` under the implemented analytics/dashboard section with the verified Memory/Knowhow cards and visible-source semantics. Do not mark admin cross-user Memory analytics complete.

- [ ] **Step 2: Run documentation and diff hygiene checks**

Run:

```bash
git diff --check
rg -n "analytics/content-overview|has_unindexed_content|content asset|内容资产" README.md README_zh.md AGENTS.md fangan_done.md
```

Expected: no whitespace errors; all four documents contain the relevant synchronized facts.

- [ ] **Step 3: Commit documentation**

```bash
git add README.md README_zh.md AGENTS.md fangan_done.md
git commit -m "docs: document content overview analytics"
```

- [ ] **Step 4: Run focused regression suites**

Run:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider -n0 backend/tests/test_content_overview_service.py backend/tests/test_content_overview_api.py backend/tests/test_knowhow_store.py backend/tests/test_knowhow_api.py backend/tests/test_knowledge_counts_cache.py backend/tests/test_notebook_summary_query.py backend/tests/test_notebook_share_copy.py backend/tests/test_scale_delta_policy.py backend/tests/test_index_build_consolidation.py -q
cd frontend && node --test app/analytics-loaders.test.mjs app/memory-navigation.test.mjs app/knowhow-model.test.mjs app/scale-index.test.mjs app/kg-build-status.test.mjs
cd frontend && npx vitest run app/content-overview-cards.component.test.tsx
```

Expected: every focused backend, Node, and component suite passes.

- [ ] **Step 5: Run the full gate twice and record the warm result**

Run:

```bash
/usr/bin/time -p env PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
/usr/bin/time -p env PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```

Expected: both runs pass; the second `real` time is at or below `60.00` seconds. The full gate includes the frontend production build.

- [ ] **Step 6: Inspect the final diff and branch**

Run:

```bash
git status --short --branch
git diff --stat origin/master...HEAD
git log --oneline --decorate origin/master..HEAD
git diff --check origin/master...HEAD
```

Expected: clean worktree, only specialty commits, and no diff whitespace errors.

- [ ] **Step 7: Push and create the single pull request**

Use `superpowers:finishing-a-development-branch` and `github:yeet`. The PR body must state:

```text
- Adds viewer-aware Memory and Knowhow cards to the existing analytics view.
- Reuses the existing Memory and Knowhow pages for all browsing and editing.
- Separates visible imported-source counts from hidden derived-content accounting.
- Preserves physical copy/storage/scheduler semantics.
- Local warm full gate: <recorded seconds>, target <=60s.
- GitHub Actions: <run URL and duration>; duration monitored only.
```

- [ ] **Step 8: Review the exact pushed head with an independent subagent**

Fully read and use `superpowers:requesting-code-review`. Dispatch a fresh review subagent against the exact pushed SHA and PR diff. Use a cost-appropriate model level for the independent review; the final exact-green review uses `gpt-5.6-terra` with high reasoning because it spans privacy, SQL/query bounds, source semantics, frontend navigation, and CI.

If the review reports Critical or Important findings, fully read and use `superpowers:receiving-code-review`, reproduce each finding, add a regression test, fix it, rerun the focused and full gates, push the new head, and request another exact-head review.

- [ ] **Step 9: Confirm GitHub Actions on the final reviewed SHA**

Wait for the Ubuntu workflow on the reviewed SHA. If it fails, fully read and use `github:gh-fix-ci`, inspect the failed step and logs, reproduce locally, and repair through the same test-first flow. If it succeeds, record job steps and duration in the PR body. Keep the check non-required unless the user explicitly approves changing branch protection.
