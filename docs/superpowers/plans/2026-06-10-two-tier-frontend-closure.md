# Two-Tier KB Frontend Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the already-built, validated two-tier (base / personal) knowledge-base features usable from the UI — add the one missing backend endpoint to set a notebook's tier, then surface tier in the UI (mark-base control, citation tier badges, edge-review queue modal).

**Architecture:** Backend gap first: `SQLiteRepository.mark_notebook_base()` already exists but there is no REST route and no symmetric "back to personal" repo method. Add `set_notebook_personal()` + a single `POST /api/notebooks/{id}/tier` route (body `{tier}`). Frontend mirrors the existing **promotion-queue** pattern exactly: a self-contained `.ts` API client (own `fetch` wrapper + `NEXT_PUBLIC_API_BASE_URL`) with a `.test.mjs` node test, plus JSX wiring in the `page.tsx` monolith. Mark-base and edge-review are both reached from the existing "分析" actions `InfoModal`; tier badges hang off the existing `CiteDetailCard` citation renderer.

**Tech Stack:** Backend — FastAPI + Pydantic, SQLite repository (`backend/app/`), pytest-style smoke via `scripts/smoke_backend.py` using `fastapi.testclient.TestClient`. Frontend — Next.js / React / TypeScript single-file `frontend/app/page.tsx` + helper `.ts` modules, each unit-tested with `node --test app/*.test.mjs`; type-checked with `tsc --noEmit`. Python interpreter: `/opt/homebrew/Caskroom/miniconda/base/bin/python`. Gate: `scripts/check.sh` (py_compile + smoke + `npm run test` + `npm run lint`).

---

## Key Facts From The Code (verified, file:line)

**Backend — tier plumbing already exists, route does not:**
- `backend/app/services/sqlite_repository.py:839-847` — `mark_notebook_base(notebook_id)`: `UPDATE notebooks SET tier='base', updated_at=? WHERE id=?`, idempotent, raises `KeyError` if missing (calls `get_notebook` first).
- **There is NO `set_notebook_personal` / `set_notebook_tier` method.** The only writes to `tier` are `mark_notebook_base` (`:845`) and `build_rx_graph(..., tier="personal")` (`:3749`, unrelated). This plan adds `set_notebook_personal()`.
- `backend/app/services/sqlite_repository.py:538-542` — `notebooks.tier` column: `TEXT NOT NULL DEFAULT 'personal'`; values are `'personal'` (default) and `'base'`.
- `backend/app/models/schemas.py:81-90` — `NotebookUpdate` does NOT include `tier`; `update_notebook` (`sqlite_repository.py:798-837`) ignores tier. So PATCH `/notebooks/{id}` cannot set tier — a dedicated endpoint is required.
- `backend/app/models/schemas.py:93-109` — `NotebookSummary` already exposes `tier: str = "personal"`.
- `backend/app/api/routes.py:544-556` — `review_relation` is the error-handling style to copy: `try / except KeyError -> 404 / except ValueError -> 400`, returns a plain `dict`. `edge_review_queue` (`:533-541`) returns `List[EdgeReviewItem]` and 404s on `KeyError`.
- `backend/app/api/routes.py:688-721` — promotion routes: same 404/400 convention, `response_model=...`.

**Backend — edge-review already complete:**
- `GET /api/notebooks/{id}/edge-review-queue` → `repository().review_queue(notebook_id, limit=100)` → `List[EdgeReviewItem]` (`routes.py:533-541`).
- `POST /api/notebooks/{id}/relations/{rel_id}/review` body `EdgeReviewRequest{status}` → `repository().set_edge_review(...)` → `{"rel_id", "review_status"}` (`routes.py:544-556`).
- `EdgeReviewItem` fields (`schemas.py:352-366`): `rel_id, notebook_id, edge_type, source_object_id, target_object_id, source_name, target_name, source_type, target_type, trust_score, edge_centrality, review_priority, review_status`.
- Allowed statuses (`sqlite_repository.py:2050`): `frozenset({"pending", "verified", "rejected"})`; bad status → `ValueError` (`:2155-2157`), missing rel → `KeyError` (`:2164-2165`).

**Backend — `AnswerAnchor.tier` already in `/ask` response:**
- `backend/app/models/schemas.py:159-171` — `AnswerAnchor` includes `tier: str = "personal"`.

**Frontend — the promotion-queue template (mirror this exactly):**
- `frontend/app/promotion-queue.ts:31-43` — self-contained API base + `apiFetch<T>` wrapper:
  ```ts
  const API_BASE =
    (typeof process !== "undefined"
      ? process.env?.NEXT_PUBLIC_API_BASE_URL
      : undefined) ?? "http://127.0.0.1:8000/api";
  async function apiFetch<T>(url: string, init?: RequestInit): Promise<T> {
    const res = await fetch(API_BASE + url, {
      headers: { "Content-Type": "application/json" },
      ...init,
    });
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    return res.json() as Promise<T>;
  }
  ```
- `frontend/app/promotion-queue.test.mjs:12-27` — `withFetchStub(run)` helper stubs `globalThis.fetch`, captures `{url, init}`, asserts URL shape + method + body. The new queue test mirrors this verbatim.
- `frontend/app/page.tsx:20-25` — imports the client fns + `type PromotionCandidate` from `./promotion-queue`.
- `frontend/app/page.tsx:813-815` — modal state: `promoQueue`, `promoOpen`, `promoBusy`.
- `frontend/app/page.tsx:1861-1892` — `openPromoQueue()` (fetch → set state → open), `decidePromotion(id, decision, reason)` (busy flag, branch on approve/reject, refetch + `loadNotebookCollection()`).
- `frontend/app/page.tsx:3002-3060` — the `{promoOpen && (...)}` modal JSX: `<section className="utility-modal" role="dialog" aria-modal="true">`, header `source-modal-header`, body lists `<article className="item">` with `tag-row` + `modal-actions` (`sort-button` reject / `new-pill` approve, both `disabled={promoBusy}`).
- `frontend/app/page.tsx:2128-2139` — the "分析" `workspace-nav-button` opens an `InfoModal` whose `actions` array already holds `{ label: "晋升队列", action: () => openPromoQueue()... }`. New entries ("设为基准库 / 取消基准", "边审查队列") go here.

**Frontend — citation rendering (where tier badges go):**
- `frontend/app/page.tsx:103-113` — local `type AnswerAnchor` does NOT carry `tier`. Must add `tier?: string`.
- `frontend/app/answer-formatting.ts:1-11` — `AnswerAnchorLike` does NOT carry `tier`. Must add `tier?: string` so `buildAnswerReferences` keeps it on `reference.anchor`.
- `frontend/app/page.tsx:3603-3621` — `referenceTitle/Snippet/Source/Location` accessors read `reference.anchor.*`. A new `referenceTier(reference)` accessor joins them.
- `frontend/app/page.tsx:3753-3784` — `CiteDetailCard` renders the per-citation detail (`cite-detail-head` with `displayLabel`, type mark, KG button; then title/snippet/source). The `[base]`/`[personal]` badge is added in the head.
- `frontend/app/page.tsx:3837-3840` — `buildAnswerReferences(answerText, answer.anchors, answer.citations)` builds references; anchors flow straight through, so once the types carry `tier` the badge has data.

**Frontend — notebook type + API helper:**
- `frontend/app/page.tsx:31-45` — `type NotebookSummary` already has `tier?: string`.
- `frontend/app/page.tsx:454-481` — `api<T>(path, options)` helper: prepends `API_BASE`, JSON headers, surfaces backend `detail` on error, returns `null` on 204. Used for in-page notebook actions (rename `:1337`, delete `:1383`, edit `:1359`).
- `frontend/app/page.tsx:763` — `currentNotebook` state (the active `NotebookSummary`, carries `tier`).
- `frontend/app/page.tsx:1030, 2386` — `currentNotebook?.tier` is already passed to the KG sidebar; reuse for the mark-base label state.

**Test + gate conventions:**
- FE unit tests: `frontend/app/answer-formatting.test.mjs` and `promotion-queue.test.mjs` use `node:test` + `node:assert/strict`, import the `.ts` module directly. `package.json`: `"test": "node --test app/*.test.mjs"`, `"lint": "tsc --noEmit"`.
- BE smoke: `scripts/smoke_backend.py` — `check_api_layer()` (`:518-616`) boots `TestClient(create_app())`, defines a local `ok(method, path, **kw)` helper, exercises routes, and asserts 404/400/422 negatives. `main()` (`:881-898`) calls each `check_*` in sequence; new assertions fold into `check_api_layer`.
- Repo doc rule (`AGENTS.md:7-13`): product-behavior changes update `README.md`, `README_zh.md`, AND `AGENTS.md` together.

---

## File Structure

**Created:**
- `frontend/app/edge-review-queue.ts` — self-contained API client for the edge-review queue (mirrors `promotion-queue.ts`): types + `fetchEdgeReviewQueue` + `reviewRelation`.
- `frontend/app/edge-review-queue.test.mjs` — node:test unit test mirroring `promotion-queue.test.mjs`.
- `frontend/app/notebook-tier.ts` — tiny tier client: `setNotebookTier(notebookId, tier)` + a `nextTier`/label pure helper for the toggle.
- `frontend/app/notebook-tier.test.mjs` — node:test unit test for the tier client + helper.

**Modified:**
- `backend/app/services/sqlite_repository.py` — add `set_notebook_personal(notebook_id)` next to `mark_notebook_base` (`~:847`).
- `backend/app/api/routes.py` — add `SetTierRequest` import + `POST /api/notebooks/{notebook_id}/tier` route near the Track-E block (`~:557`).
- `backend/app/models/schemas.py` — add `class SetTierRequest(BaseModel)` near `EdgeReviewRequest` (`~:372`).
- `scripts/smoke_backend.py` — extend `check_api_layer` (`:518-616`) with tier + edge-review-queue assertions.
- `frontend/app/page.tsx` — imports; tier toggle handler + actions-menu entries; edge-review modal state + handlers + `{edgeReviewOpen && (...)}` JSX + actions-menu entry; `AnswerAnchor` type `tier`; `referenceTier` accessor + badge in `CiteDetailCard`.
- `frontend/app/answer-formatting.ts` — `AnswerAnchorLike.tier?: string`.
- `README.md`, `README_zh.md`, `AGENTS.md` — document the tier endpoint + the three new UI affordances.

---

## Task 1: Backend — `set_notebook_personal` repo method

**Files:**
- Modify: `backend/app/services/sqlite_repository.py:839-847` (add method directly after `mark_notebook_base`)
- Test: `scripts/smoke_backend.py:881-898` (`main`, add a repository-level check block) — see Step 1

- [ ] **Step 1: Write the failing test**

Add this block inside `main()` in `scripts/smoke_backend.py`, immediately after the `manual_nb = repository.create_notebook(...)` line (currently `:917-918`):

```python
        # Two-tier: tier toggle round-trips on the repository.
        assert repository.get_notebook(manual_nb.id).tier == "personal"
        repository.mark_notebook_base(manual_nb.id)
        assert repository.get_notebook(manual_nb.id).tier == "base"
        repository.set_notebook_personal(manual_nb.id)
        assert repository.get_notebook(manual_nb.id).tier == "personal"
        try:
            repository.set_notebook_personal("nb-missing")
            raise AssertionError("set_notebook_personal should raise KeyError on missing notebook")
        except KeyError:
            pass
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python scripts/smoke_backend.py`
Expected: FAIL — `AttributeError: 'SQLiteRepository' object has no attribute 'set_notebook_personal'`.

- [ ] **Step 3: Implement the method**

In `backend/app/services/sqlite_repository.py`, immediately after `mark_notebook_base` (after line 847), add:

```python
    def set_notebook_personal(self, notebook_id: str) -> None:
        """Reset a notebook back to the personal tier (tier='personal').
        Symmetric inverse of mark_notebook_base; idempotent.
        Raises KeyError if the notebook does not exist."""
        self.get_notebook(notebook_id)  # raises KeyError if missing
        with self._write() as db:
            db.execute(
                "UPDATE notebooks SET tier='personal', updated_at=? WHERE id=?",
                (_now(), notebook_id),
            )
```

- [ ] **Step 4: Run to verify it passes**

Run: `PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python scripts/smoke_backend.py`
Expected: PASS (script exits 0, prints its existing success line).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py scripts/smoke_backend.py
git commit -m "feat(repo): add set_notebook_personal (inverse of mark_notebook_base)"
```

---

## Task 2: Backend — `POST /api/notebooks/{id}/tier` endpoint

**Files:**
- Modify: `backend/app/models/schemas.py:369-372` (add `SetTierRequest` after `EdgeReviewRequest`)
- Modify: `backend/app/api/routes.py` (import + route near `:557`)
- Test: `scripts/smoke_backend.py:518-616` (`check_api_layer`)

- [ ] **Step 1: Write the failing test**

In `scripts/smoke_backend.py`, inside `check_api_layer`, after the notebook `GET` assertions (currently `:570-571`, the `ok("GET", f"/api/notebooks/{nid}")` / `sources == []` lines), add:

```python
        # Two-tier: set tier to base, then back to personal, via the REST route.
        assert ok("GET", f"/api/notebooks/{nid}")["tier"] == "personal"
        set_base = ok("POST", f"/api/notebooks/{nid}/tier", json={"tier": "base"})
        assert set_base["tier"] == "base"
        assert ok("GET", f"/api/notebooks/{nid}")["tier"] == "base"
        set_personal = ok("POST", f"/api/notebooks/{nid}/tier", json={"tier": "personal"})
        assert set_personal["tier"] == "personal"
        # Bad tier -> 400; missing notebook -> 404.
        assert client.post(f"/api/notebooks/{nid}/tier", json={"tier": "bogus"}).status_code == 400
        assert client.post("/api/notebooks/nb-missing/tier", json={"tier": "base"}).status_code == 404
```

- [ ] **Step 2: Run to verify it fails**

Run: `PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python scripts/smoke_backend.py`
Expected: FAIL — the first `ok("POST", .../tier ...)` asserts `status_code == 200` but the route returns `405`/`404` (no such route).

- [ ] **Step 3: Add the request schema**

In `backend/app/models/schemas.py`, directly after `EdgeReviewRequest` (after line 371), add:

```python
class SetTierRequest(BaseModel):
    """Payload for POST /notebooks/{id}/tier."""
    tier: str   # "base" | "personal"
```

- [ ] **Step 4: Implement the route**

In `backend/app/api/routes.py`, add `SetTierRequest` to the schema import block (the `from app.models.schemas import (...)` group that already includes `EdgeReviewRequest`). Then, directly after `review_relation` ends (after line 556), add:

```python
@router.post("/notebooks/{notebook_id}/tier", response_model=NotebookSummary)
def set_notebook_tier(notebook_id: str, payload: SetTierRequest) -> NotebookSummary:
    """Set a notebook's federation tier: 'base' (authoritative reference KG)
    or 'personal' (default user notes). Drives tier-weighted relevance and
    conflict precedence in ask()."""
    tier = payload.tier.strip().lower()
    if tier not in {"base", "personal"}:
        raise HTTPException(status_code=400, detail="tier must be 'base' or 'personal'")
    try:
        if tier == "base":
            repository().mark_notebook_base(notebook_id)
        else:
            repository().set_notebook_personal(notebook_id)
        return repository().get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
```

> `NotebookSummary` is already imported in `routes.py` (used by the existing notebook routes). Confirm it is in the import block; if not, add it alongside `SetTierRequest`.

- [ ] **Step 5: Run to verify it passes**

Run: `PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python scripts/smoke_backend.py`
Expected: PASS.

- [ ] **Step 6: Run py_compile gate on the two changed modules**

Run: `/opt/homebrew/Caskroom/miniconda/base/bin/python -m py_compile backend/app/api/routes.py backend/app/models/schemas.py`
Expected: no output, exit 0.

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/routes.py backend/app/models/schemas.py scripts/smoke_backend.py
git commit -m "feat(api): POST /notebooks/{id}/tier to set base/personal tier"
```

---

## Task 3: Frontend — `notebook-tier.ts` API client + pure toggle helper

**Files:**
- Create: `frontend/app/notebook-tier.ts`
- Test: `frontend/app/notebook-tier.test.mjs`

- [ ] **Step 1: Write the failing test**

Create `frontend/app/notebook-tier.test.mjs` (mirrors `promotion-queue.test.mjs:12-27` fetch-stub style):

```js
import test from "node:test";
import assert from "node:assert/strict";

import { setNotebookTier, nextTier, tierLabel } from "./notebook-tier.ts";

function withFetchStub(run) {
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return { ok: true, status: 200, json: async () => ({}), text: async () => "" };
  };
  return Promise.resolve(run(calls)).finally(() => {
    globalThis.fetch = original;
  });
}

test("nextTier flips between base and personal", () => {
  assert.strictEqual(nextTier("personal"), "base");
  assert.strictEqual(nextTier("base"), "personal");
  assert.strictEqual(nextTier(undefined), "base");
});

test("tierLabel describes the toggle action for the current tier", () => {
  assert.strictEqual(tierLabel("personal"), "设为基准库");
  assert.strictEqual(tierLabel("base"), "取消基准库");
});

test("setNotebookTier POSTs /notebooks/{id}/tier with the tier body", () =>
  withFetchStub(async (calls) => {
    await setNotebookTier("nb-1", "base");
    assert.match(calls[0].url, /\/notebooks\/nb-1\/tier$/);
    assert.strictEqual(calls[0].init.method, "POST");
    assert.deepStrictEqual(JSON.parse(calls[0].init.body), { tier: "base" });
  }));
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && node --test app/notebook-tier.test.mjs`
Expected: FAIL — cannot resolve `./notebook-tier.ts`.

- [ ] **Step 3: Implement the client**

Create `frontend/app/notebook-tier.ts` (self-contained, mirrors `promotion-queue.ts:31-72`):

```ts
// Two-tier federation — notebook tier client (pure logic, unit-tested in
// notebook-tier.test.mjs). Self-contained fetch wrapper so it runs under
// `node --test` without importing the React page module.

export type NotebookTier = "base" | "personal";

export type NotebookSummaryLike = {
  id: string;
  name: string;
  tier?: string;
  [k: string]: unknown;
};

const API_BASE =
  (typeof process !== "undefined"
    ? process.env?.NEXT_PUBLIC_API_BASE_URL
    : undefined) ?? "http://127.0.0.1:8000/api";

async function apiFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(API_BASE + url, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json() as Promise<T>;
}

export const nextTier = (current?: string): NotebookTier =>
  current === "base" ? "personal" : "base";

export const tierLabel = (current?: string): string =>
  current === "base" ? "取消基准库" : "设为基准库";

export const setNotebookTier = (
  notebookId: string,
  tier: NotebookTier
): Promise<NotebookSummaryLike> =>
  apiFetch(`/notebooks/${notebookId}/tier`, {
    method: "POST",
    body: JSON.stringify({ tier }),
  });
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd frontend && node --test app/notebook-tier.test.mjs`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/app/notebook-tier.ts frontend/app/notebook-tier.test.mjs
git commit -m "feat(fe): notebook-tier API client + tier toggle helper"
```

---

## Task 4: Frontend — wire the mark-base toggle into the actions menu

**Files:**
- Modify: `frontend/app/page.tsx:20-25` (import), `:763` (state usage), `:1861` (add handler near governance handlers), `:2128-2139` (actions-menu entry)

- [ ] **Step 1: Add the import**

In `frontend/app/page.tsx`, after the promotion-queue import block (`:25`), add:

```ts
import { setNotebookTier, nextTier, tierLabel } from "./notebook-tier";
```

- [ ] **Step 2: Add the toggle handler**

In `page.tsx`, directly before `openPromoQueue` (currently `:1862`), add:

```ts
  // --- Two-tier federation: mark notebook base / personal -----------------
  async function toggleNotebookTier() {
    if (!currentNotebook) return;
    const target = nextTier(currentNotebook.tier);
    const updated = await setNotebookTier(currentNotebook.id, target);
    setCurrentNotebook(updated);
    await loadNotebookCollection();
    setToast(
      target === "base"
        ? "已设为基准库（base）— 该 KG 将作为权威参考层参与检索与冲突仲裁"
        : "已取消基准库，恢复为个人层（personal）"
    );
  }
```

> `setCurrentNotebook` is the existing setter for the `currentNotebook` state (`:763`). `loadNotebookCollection` and `setToast` are already used in `decidePromotion` (`:1888, :1880`). `setNotebookTier` returns `NotebookSummaryLike`; assigning to `currentNotebook` is structurally compatible with `NotebookSummary` (both carry `id/name/tier`); if `tsc` complains, cast: `setCurrentNotebook(updated as NotebookSummary)`.

- [ ] **Step 3: Add the actions-menu entry**

In the "分析" `InfoModal` actions array (`:2131-2138`), add an entry after the existing `{ label: "晋升队列", ... }` line (`:2137`):

```tsx
                    { label: tierLabel(currentNotebook?.tier), action: () => toggleNotebookTier().catch(reportError) }
```

- [ ] **Step 4: Type-check + unit tests (no behavior test for JSX wiring)**

Run: `cd frontend && npm run lint && npm run test`
Expected: `tsc --noEmit` clean; all `node --test` files pass (including `notebook-tier.test.mjs`).

> The wiring itself (button → handler → client) is exercised by the `setNotebookTier` unit test (Task 3) plus the backend round-trip (Task 2). Per repo convention testable logic lives in the `.ts` helper; the `page.tsx` JSX is verified by `tsc`.

- [ ] **Step 5: Commit**

```bash
git add frontend/app/page.tsx
git commit -m "feat(fe): mark-base/personal toggle in notebook actions menu"
```

---

## Task 5: Frontend — `tier` field on anchor types

**Files:**
- Modify: `frontend/app/answer-formatting.ts:1-11` (`AnswerAnchorLike`)
- Modify: `frontend/app/page.tsx:103-113` (`type AnswerAnchor`)
- Test: `frontend/app/answer-formatting.test.mjs` (extend an existing assertion)

- [ ] **Step 1: Write the failing test**

In `frontend/app/answer-formatting.test.mjs`, extend the first anchor in the `anchors` fixture (`:10-19`) to carry a tier, and assert it survives `buildAnswerReferences`. Replace the `k2` anchor object (`:11-19`) with:

```js
  {
    key: "k2",
    object_id: "ko-2",
    object_type: "claim",
    label: "Claim",
    name: "Second claim",
    source_title: "source.md",
    location_label: "L2",
    tier: "base",
  },
```

Then add a new test at the end of the file:

```js
test("preserves anchor tier on built references", () => {
  const references = buildAnswerReferences("看 [k2]。", anchors, []);
  assert.equal(references[0].anchor?.tier, "base");
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && node --test app/answer-formatting.test.mjs`
Expected: the runtime test still passes (JS ignores unknown fields), but `npm run lint` (next step) is the real gate. Run lint now to see the type error:

Run: `cd frontend && npm run lint`
Expected: FAIL — `tsc` errors that `tier` is not assignable to `AnswerAnchorLike` (object literal in the test would be fine, but the new `references[0].anchor?.tier` access errors: `Property 'tier' does not exist on type 'AnswerAnchorLike'`).

- [ ] **Step 3: Add `tier` to both anchor types**

In `frontend/app/answer-formatting.ts`, in `AnswerAnchorLike` (`:1-11`), add after `location_label?: string;`:

```ts
  tier?: string;
```

In `frontend/app/page.tsx`, in `type AnswerAnchor` (`:103-113`), add after `location_label: string;`:

```ts
  tier?: string;
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd frontend && node --test app/answer-formatting.test.mjs && npm run lint`
Expected: tests PASS; `tsc` clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/app/answer-formatting.ts frontend/app/page.tsx frontend/app/answer-formatting.test.mjs
git commit -m "feat(fe): carry tier on AnswerAnchor types for citation badges"
```

---

## Task 6: Frontend — `[base]`/`[personal]` badge on citations

**Files:**
- Modify: `frontend/app/page.tsx:3603-3621` (add `referenceTier` accessor), `:3753-3784` (`CiteDetailCard` head)

- [ ] **Step 1: Add the tier accessor**

In `frontend/app/page.tsx`, after `referenceLocation` (`:3618-3621`), add:

```tsx
function referenceTier(reference: AnswerReference): string {
  return reference.anchor?.tier || "";
}
```

- [ ] **Step 2: Render the badge in `CiteDetailCard`**

In `CiteDetailCard` (`:3764-3784`), inside the `cite-detail-head` div, add the badge after the existing object-type span (`:3768`). Compute `const tier = referenceTier(reference);` just below the other `const ...` lines (`:3759-3763`), then in the head:

```tsx
        {tier && (
          <span className={`tier-badge tier-${tier}`} title={tier === "base" ? "来自基准库（权威参考层）" : "来自个人层"}>
            {tier === "base" ? "base" : "personal"}
          </span>
        )}
```

So the head block becomes (showing the surrounding context already at `:3766-3778`):

```tsx
      <div className="cite-detail-head">
        <strong>{reference.displayLabel}</strong>
        {objectType && <span><KgTypeMark type={objectType} />{kgTypeLabel(objectType)}</span>}
        {tier && (
          <span className={`tier-badge tier-${tier}`} title={tier === "base" ? "来自基准库（权威参考层）" : "来自个人层"}>
            {tier === "base" ? "base" : "personal"}
          </span>
        )}
        <button
          type="button"
          onClick={() => onOpenKnowledgeGraph(reference.anchor?.object_id)}
          disabled={!reference.anchor?.object_id}
          title={reference.anchor?.object_id ? "在知识图谱中定位" : "该引用没有绑定知识节点"}
        >
          <ExternalLink size={14} />
          知识图谱
        </button>
      </div>
```

- [ ] **Step 3: Add badge styling**

In `frontend/app/globals.css`, append a small rule (no exact line — add at end of file):

```css
.tier-badge {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 999px;
  border: 1px solid var(--border, #d0d0d0);
  text-transform: uppercase;
  letter-spacing: 0.03em;
}
.tier-badge.tier-base { background: #eef6ff; color: #1457b8; border-color: #bcd9ff; }
.tier-badge.tier-personal { background: #f4f4f5; color: #555; }
```

> If `globals.css` defines design tokens, prefer reusing an existing accent token over the hardcoded hex; the badge must remain legible in the existing theme. Styling is cosmetic — not gated by a test.

- [ ] **Step 4: Type-check**

Run: `cd frontend && npm run lint`
Expected: `tsc` clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/app/page.tsx frontend/app/globals.css
git commit -m "feat(fe): show base/personal tier badge on Ask citations"
```

---

## Task 7: Frontend — `edge-review-queue.ts` API client

**Files:**
- Create: `frontend/app/edge-review-queue.ts`
- Test: `frontend/app/edge-review-queue.test.mjs`

- [ ] **Step 1: Write the failing test**

Create `frontend/app/edge-review-queue.test.mjs` (mirrors `promotion-queue.test.mjs` exactly):

```js
import test from "node:test";
import assert from "node:assert/strict";

import { fetchEdgeReviewQueue, reviewRelation } from "./edge-review-queue.ts";

function withFetchStub(run) {
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return { ok: true, status: 200, json: async () => ({}), text: async () => "" };
  };
  return Promise.resolve(run(calls)).finally(() => {
    globalThis.fetch = original;
  });
}

test("exports the two client functions", () => {
  assert.strictEqual(typeof fetchEdgeReviewQueue, "function");
  assert.strictEqual(typeof reviewRelation, "function");
});

test("fetchEdgeReviewQueue GETs the edge-review-queue with no limit", () =>
  withFetchStub(async (calls) => {
    await fetchEdgeReviewQueue("nb-1");
    assert.match(calls[0].url, /\/notebooks\/nb-1\/edge-review-queue$/);
  }));

test("fetchEdgeReviewQueue appends the limit query param", () =>
  withFetchStub(async (calls) => {
    await fetchEdgeReviewQueue("nb-1", 25);
    assert.match(calls[0].url, /\/notebooks\/nb-1\/edge-review-queue\?limit=25$/);
  }));

test("reviewRelation POSTs the review endpoint with the status body", () =>
  withFetchStub(async (calls) => {
    await reviewRelation("nb-1", "rel-9", "verified");
    assert.match(calls[0].url, /\/notebooks\/nb-1\/relations\/rel-9\/review$/);
    assert.strictEqual(calls[0].init.method, "POST");
    assert.deepStrictEqual(JSON.parse(calls[0].init.body), { status: "verified" });
  }));
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd frontend && node --test app/edge-review-queue.test.mjs`
Expected: FAIL — cannot resolve `./edge-review-queue.ts`.

- [ ] **Step 3: Implement the client**

Create `frontend/app/edge-review-queue.ts` (types from `schemas.py:352-366`; wrapper from `promotion-queue.ts:31-43`):

```ts
// Track E — edge curation / review-queue API client (pure logic, unit-tested
// in edge-review-queue.test.mjs). Self-contained fetch wrapper so it runs under
// `node --test` without importing the React page module. Mirrors promotion-queue.ts.

export type EdgeReviewStatus = "pending" | "verified" | "rejected";

export type EdgeReviewItem = {
  rel_id: string;
  notebook_id: string;
  edge_type: string;
  source_object_id: string;
  target_object_id: string;
  source_name: string;
  target_name: string;
  source_type: string;
  target_type: string;
  trust_score: number;
  edge_centrality: number;
  review_priority: number;
  review_status: EdgeReviewStatus;
};

export type ReviewRelationResult = {
  rel_id: string;
  review_status: EdgeReviewStatus;
};

const API_BASE =
  (typeof process !== "undefined"
    ? process.env?.NEXT_PUBLIC_API_BASE_URL
    : undefined) ?? "http://127.0.0.1:8000/api";

async function apiFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(API_BASE + url, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json() as Promise<T>;
}

export const fetchEdgeReviewQueue = (
  notebookId: string,
  limit?: number
): Promise<EdgeReviewItem[]> =>
  apiFetch(
    `/notebooks/${notebookId}/edge-review-queue${limit != null ? `?limit=${limit}` : ""}`
  );

export const reviewRelation = (
  notebookId: string,
  relId: string,
  status: EdgeReviewStatus
): Promise<ReviewRelationResult> =>
  apiFetch(
    `/notebooks/${notebookId}/relations/${encodeURIComponent(relId)}/review`,
    { method: "POST", body: JSON.stringify({ status }) }
  );
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd frontend && node --test app/edge-review-queue.test.mjs`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add frontend/app/edge-review-queue.ts frontend/app/edge-review-queue.test.mjs
git commit -m "feat(fe): edge-review-queue API client (mirrors promotion-queue)"
```

---

## Task 8: Frontend — edge-review queue modal + actions wiring

**Files:**
- Modify: `frontend/app/page.tsx:20-25` (import), `:813-815` (state, after promo state), `:1861-1892` (handlers, after governance handlers), `:2128-2139` (actions-menu entry), `:3060` (modal JSX, after the promo modal closes)

- [ ] **Step 1: Add the import**

In `page.tsx`, after the `notebook-tier` import added in Task 4, add:

```ts
import { fetchEdgeReviewQueue, reviewRelation, type EdgeReviewItem } from "./edge-review-queue";
```

- [ ] **Step 2: Add modal state**

After the promo state (`:813-815`), add:

```ts
  const [edgeQueue, setEdgeQueue] = useState<EdgeReviewItem[] | null>(null);
  const [edgeReviewOpen, setEdgeReviewOpen] = useState(false);
  const [edgeBusy, setEdgeBusy] = useState(false);
```

- [ ] **Step 3: Add the open + decide handlers**

After `decidePromotion` (`:1892`), add (mirrors `openPromoQueue`/`decidePromotion`):

```ts
  // --- Track E: edge review queue ----------------------------------------
  async function openEdgeReviewQueue() {
    if (!currentNotebookId) return;
    const queue = await fetchEdgeReviewQueue(currentNotebookId);
    setEdgeQueue(queue);
    setEdgeReviewOpen(true);
  }

  async function decideEdge(relId: string, status: "verified" | "rejected") {
    if (!currentNotebookId) return;
    setEdgeBusy(true);
    try {
      await reviewRelation(currentNotebookId, relId, status);
      setToast(status === "verified" ? "关系已确认" : "关系已拒绝，后续图推理将忽略它");
      const queue = await fetchEdgeReviewQueue(currentNotebookId);
      setEdgeQueue(queue);
    } finally {
      setEdgeBusy(false);
    }
  }
```

- [ ] **Step 4: Add the actions-menu entry**

In the "分析" `InfoModal` actions array (`:2131-2138`), after the tier toggle entry added in Task 4, add:

```tsx
                    { label: "边审查队列", action: () => openEdgeReviewQueue().catch(reportError) }
```

- [ ] **Step 5: Add the modal JSX**

After the promotion-queue modal block closes (`:3060`, the `)}` that ends `{promoOpen && (...)}`), add (mirrors the promo modal at `:3002-3060`):

```tsx
      {edgeReviewOpen && (
        <section
          className="utility-modal"
          role="dialog"
          aria-modal="true"
          onClick={(event) => { if (event.currentTarget === event.target) setEdgeReviewOpen(false); }}
        >
          <div className="utility-modal-card">
            <div className="source-modal-header">
              <div>
                <h2>边审查队列</h2>
                <p>按「高中心性 × 低可信」排序的关系。确认可信边，或拒绝错误边（被拒边将从所有图推理遍历中排除）。</p>
              </div>
              <button className="icon-button" onClick={() => setEdgeReviewOpen(false)} title="Close">×</button>
            </div>
            <div className="source-detail-body">
              {(edgeQueue ?? []).length === 0 ? (
                <p className="tool-hint">暂无待审关系。</p>
              ) : (
                <div className="stack">
                  {(edgeQueue ?? []).map((edge) => (
                    <article className="item" key={edge.rel_id}>
                      <div className="tag-row">
                        <span className="tag">{edge.edge_type}</span>
                        <span className="tag">{edge.review_status}</span>
                        <span className="tag">可信 {edge.trust_score.toFixed(2)}</span>
                        <span className="tag">优先级 {edge.review_priority.toFixed(2)}</span>
                      </div>
                      <h3>{(edge.source_name || edge.source_object_id)} → {(edge.target_name || edge.target_object_id)}</h3>
                      {edge.review_status !== "rejected" && (
                        <div className="modal-actions">
                          <button
                            className="sort-button"
                            disabled={edgeBusy}
                            onClick={() => decideEdge(edge.rel_id, "rejected").catch(reportError)}
                          >
                            拒绝
                          </button>
                          <button
                            className="new-pill"
                            disabled={edgeBusy}
                            onClick={() => decideEdge(edge.rel_id, "verified").catch(reportError)}
                          >
                            确认可信
                          </button>
                        </div>
                      )}
                    </article>
                  ))}
                </div>
              )}
            </div>
          </div>
        </section>
      )}
```

- [ ] **Step 6: Run the full FE gate**

Run: `cd frontend && npm run test && npm run lint`
Expected: all `node --test` files pass; `tsc --noEmit` clean.

- [ ] **Step 7: Commit**

```bash
git add frontend/app/page.tsx
git commit -m "feat(fe): edge-review queue modal with approve/reject actions"
```

---

## Task 9: Docs — README / README_zh / AGENTS

**Files:**
- Modify: `README.md`, `README_zh.md`, `AGENTS.md`

Per `AGENTS.md:7-13`, product-behavior changes update all three docs together.

- [ ] **Step 1: Locate the API / features section in each doc**

Run: `grep -n "promotion-queue\|edge-review\|tier\|晋升\|/notebooks/" README.md README_zh.md AGENTS.md`
Expected: find the existing endpoint list / feature section where the two-tier and governance features are described (promotion queue, edge curation).

- [ ] **Step 2: Add the tier endpoint + UI affordances**

In each of `README.md`, `README_zh.md`, `AGENTS.md`, in the API/features section, document:
- `POST /api/notebooks/{id}/tier` body `{tier: "base" | "personal"}` → returns the updated `NotebookSummary`; 400 on bad tier, 404 on missing notebook. Sets the notebook's federation tier (base = authoritative reference KG, personal = default user notes).
- UI: notebook actions menu now offers "设为基准库 / 取消基准库" (mark a notebook as the base KG and back).
- UI: Ask citations now show a `base`/`personal` tier badge per cited anchor.
- UI: notebook actions menu now offers "边审查队列" — an edge-review modal (confirm / reject relations; rejected edges are excluded from graph reasoning), backed by the existing `GET /edge-review-queue` + `POST /relations/{rel_id}/review` endpoints.

Keep `README.md` (English) and `README_zh.md` (Chinese) content-equivalent; `AGENTS.md` notes it under the product-flow/API contract section.

- [ ] **Step 3: Commit**

```bash
git add README.md README_zh.md AGENTS.md
git commit -m "docs: two-tier UI (mark-base, citation tier badges, edge-review) + tier endpoint"
```

---

## Task 10: Full gate — `scripts/check.sh`

**Files:** none (verification only)

- [ ] **Step 1: Run the full check**

Run: `bash scripts/check.sh`
Expected: PASS end-to-end —
- `py_compile` of all listed backend modules (includes `routes.py`, `schemas.py`, `sqlite_repository.py`) — exit 0.
- `markdown_it` / `numpy` import probe — exit 0.
- `scripts/smoke_backend.py` — prints its success line, exit 0 (now exercises the tier endpoint + repo round-trip).
- `frontend`: `npm run test` (`node --test app/*.test.mjs`) — all suites pass, including `notebook-tier.test.mjs`, `edge-review-queue.test.mjs`, updated `answer-formatting.test.mjs`.
- `frontend`: `npm run lint` (`tsc --noEmit`) — clean.

> If `frontend/node_modules` is absent, the script skips FE lint/test with a notice. Ensure deps are installed (`cd frontend && npm install`) so the FE gate actually runs.

- [ ] **Step 2: Final commit (if any uncommitted state)**

```bash
git add -A && git commit -m "chore: two-tier frontend closure — green check.sh" || echo "nothing to commit"
```

---

## Self-Review Notes

- **Spec coverage:** (1) BE tier endpoint → Tasks 1-2; (2) FE mark-base control → Tasks 3-4; (3) FE tier badges → Tasks 5-6; (4) FE edge-review queue → Tasks 7-8; docs → Task 9; gate → Task 10. Sequence honors the dependency (BE endpoint before FE consumers).
- **set-personal repo method:** confirmed absent (`sqlite_repository.py` only writes `tier='base'` at `:845`); Task 1 adds the symmetric `set_notebook_personal`.
- **Type consistency:** `setNotebookTier` returns `NotebookSummaryLike`; `EdgeReviewItem` FE type matches `schemas.py:352-366` field-for-field; `EdgeReviewStatus` matches `_REVIEW_STATUSES` (`:2050`); `nextTier`/`tierLabel`/`referenceTier` names are used consistently across Tasks 3/4/6.
- **Pattern fidelity:** both new FE clients copy `promotion-queue.ts`'s self-contained `API_BASE`+`apiFetch` (no import of `page.tsx`), and both `.test.mjs` copy the `withFetchStub` harness — so they run under `node --test` in isolation, matching the repo convention.
- **Endpoint shape:** single `POST /api/notebooks/{id}/tier` body `{tier}` chosen over `/mark-base` because it sets base AND personal symmetrically and returns the updated `NotebookSummary` (so the FE can update `currentNotebook` in one call); error convention copied from `review_relation` (`routes.py:544-556`).
