# Track F — Governance / Promotion Workflow — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An owner-triggered *propose → under_review → approved/rejected* state machine that promotes a personal-KG node into the base corpus with deduplication (reusing `kg_merge` clustering), plus a strong-review gate for direct base additions, and a matching curator UI.

**Date:** 2026-06-10  
**Branch / worktree:** `claude/wave2` (off `994dd14`)  
**Python:** `/opt/homebrew/Caskroom/miniconda/base/bin/python`  
**Run tests from:** `backend/`

---

## Background & architecture constraints (read before executing)

### Existing governance primitives to build on

| Primitive | Location | Key fact |
|---|---|---|
| `USABLE_STATUSES` | `sqlite_repository.py:121` | `("approved","reviewed","project_specific","conflict")` — `deprecated` excluded; **these are the status values that flow into retrieval** |
| `KNOWLEDGE_STATUSES` | `sqlite_repository.py:127` | Full set: `("approved","reviewed","deprecated","conflict","project_specific")` — `update_knowledge` validates against this at line 1771 |
| `update_knowledge` | `sqlite_repository.py:2760-2811` | Stamps `last_reviewed` when `status` changes; re-embeds payload; invalidates unified cache |
| `concept_merge_candidates` DDL | `sqlite_repository.py:413-420` | `(id, notebook_id, canonical_a, canonical_b, score, status, created_at, updated_at)` + `confidence`, `rationale`, `reviewed_by` (migration-added at line 504-511); statuses: `pending / confirmed / rejected` |
| `confirm_merge` / `reject_merge` | `sqlite_repository.py:2030-2040` | Calls `set_merge_decision` → marks dirty → invalidates unified cache |
| `review_pending_merges` | `sqlite_repository.py:2042-2073` | LLM-assisted batch review pattern; `reviewed_by='llm'` stamp |
| `approve_derived_rule` | `sqlite_repository.py:2679-2739` | Best precedent for promotion: idempotency guard (`source_candidate_id`), inserts into `knowledge_objects` at `status='approved'`, stamps candidate as `'approved'`; raises `ValueError` if already rejected |
| `cluster_objects` | `kg_merge.py:179-258` | Generic dedup engine (`confirmed` / `rejected` `frozenset` args); call it against base-corpus objects to find overlap before promotion |
| `mark_notebook_base` | `sqlite_repository.py:788-796` | Sets `tier='base'` on a notebook; `NotebookSummary.tier` field present since Wave 1-B (`schemas.py:109`) |
| `store_kg` | `sqlite_repository.py:1917-1954` | Direct-writes objects at `status='approved'` regardless of notebook tier — **no gate exists yet** |

### Existing route patterns to mirror

| Pattern | Example route | Handler shape |
|---|---|---|
| Async approve/reject of a candidate | `POST /notebooks/{nb}/derived-rules/{id}/approve` | `routes.py:504-511` — calls `repo.approve_*`, returns model, raises `HTTPException(404/400)` |
| Queue listing | `GET /notebooks/{nb}/derived-rules` | `routes.py:496-501` — query-string filter optional, returns `List[Model]` |
| Paired confirm/reject | `POST …/merges/{id}/confirm` + `…/reject` | `routes.py:588-603` — returns `{"ok": True}` |
| Merge review (LLM batch) | `POST /notebooks/{nb}/unified-kg/merges/review` | `routes.py:624-633` — request body `MergeReviewRequest`, response `MergeReviewSummary` |

### Frontend patterns to mirror

- `page.tsx` is one large client component (~3 790 lines). All API interactions via the `api<T>(url, init?)` helper (line ~535+).
- Candidate queues use `useState<T[] | null>` with an `open` boolean for a `.utility-modal` dialog (e.g. `derivedRules` + `derivedOpen` at lines 802-803).
- Approve/reject actions call `decideDerivedRule(id, "approve"|"reject")` which calls the endpoint and refreshes state (lines 1836-1847).
- The existing `DerivedRuleCandidate` type (lines 230-240) and `PendingMerge` type (line 267) show the expected shape.
- Frontend test convention: pure-logic modules extracted to `app/*.ts` get a `app/*.test.mjs` using Node `test`/`assert` (no React testing lib); React components are not unit-tested — manual build + visual check is the gate.

### Critical invariants (must not regress)

1. **`[0,1]` relevance / tau calibration** — promotion approval copies an object to the base notebook; it goes through the same `store_kg` / `_embed_knowledge` path; no score rescaling needed.
2. **`USABLE_STATUSES` retrieval gate** — promoted objects in the base corpus must carry `status='approved'` (or another usable status) so they surface in `_knowledge_objects` queries.
3. **`tier_weight`** — the base-corpus tier weighting (imported at `sqlite_repository.py:111`) applies to the base notebook's objects automatically once they are stored there; no extra wiring needed.

---

## Files touched by this plan

**New files:**
- `backend/tests/test_trackF_governance_promotion.py` — all backend tests (Tasks 1–3)
- `frontend/app/promotion-queue.ts` — pure API-client logic for promotion endpoints

**Modified files:**
- `backend/app/services/sqlite_repository.py` — new DDL table + migration, 5 new repo methods, `store_kg` base guard
- `backend/app/models/schemas.py` — 3 new Pydantic models (`PromotionCandidate`, `PromotionApproveResult`, `PromotionRejectRequest`)
- `backend/app/api/routes.py` — 4 new endpoints
- `frontend/app/page.tsx` — promotion queue modal + API calls (Task 4)

---

## Task 1 — Promotion state machine (backend repo + DDL)

**Files:**
- Create: `backend/tests/test_trackF_governance_promotion.py`
- Modify: `backend/app/services/sqlite_repository.py`
- Modify: `backend/app/models/schemas.py`

### Context

`concept_merge_candidates` (line 413) tracks intra-notebook concept pairs; it is not the right home for cross-tier promotions (different semantic scope and different actor). Add a dedicated `promotion_candidates` table to avoid conflating two orthogonal workflows. The `approve_derived_rule` pattern (lines 2679-2739) is the exact template: idempotency via `source_candidate_id`, single-transaction insert into `knowledge_objects`, candidate status stamp.

### Steps

- [ ] **Step 1: Write failing tests** — create `backend/tests/test_trackF_governance_promotion.py`:

  ```python
  # Test class skeleton (fill in bodies):
  class TestPromotionStateMachine:
      def test_propose_creates_candidate_in_proposed_state(self, repo): ...
      def test_propose_object_not_in_personal_notebook_raises(self, repo): ...
      def test_propose_object_already_proposed_is_idempotent(self, repo): ...
      def test_list_promotion_queue_returns_only_under_review_and_proposed(self, repo): ...
      def test_approve_promotion_copies_object_to_base_corpus(self, repo): ...
      def test_approve_promotion_sets_base_object_status_approved(self, repo): ...
      def test_approve_promotion_is_idempotent(self, repo): ...
      def test_approve_promotion_deduplicates_against_existing_base_objects(self, repo): ...
      def test_reject_promotion_leaves_personal_object_untouched(self, repo): ...
      def test_reject_promotion_records_reason_on_candidate(self, repo): ...
      def test_rejected_object_does_not_appear_in_base_corpus(self, repo): ...

  class TestBaseStrongReviewGate:
      def test_store_kg_to_base_notebook_inserts_as_reviewed_not_approved(self, repo): ...
      def test_store_kg_to_personal_notebook_still_inserts_as_approved(self, repo): ...
  ```

  Use the same `repo` fixture from `test_cross_doc_merge.py` lines 20-27 (tmp_path + `FakeEmbedder`).

- [ ] **Step 2: Add `promotion_candidates` table to `_migrate()`** in `sqlite_repository.py`, after the `concept_merge_candidates` block (after line 420):

  ```sql
  CREATE TABLE IF NOT EXISTS promotion_candidates (
    id TEXT PRIMARY KEY,
    notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
    object_id TEXT NOT NULL,
    object_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    -- status values: proposed | under_review | approved | rejected
    reason TEXT NOT NULL DEFAULT '',
    reviewed_by TEXT NOT NULL DEFAULT '',
    base_match_id TEXT NOT NULL DEFAULT '',
    -- canonical_id in the base corpus if dedup found a match
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
  );
  CREATE INDEX IF NOT EXISTS idx_promotion_status ON promotion_candidates(status);
  CREATE INDEX IF NOT EXISTS idx_promotion_nb ON promotion_candidates(notebook_id, status);
  CREATE UNIQUE INDEX IF NOT EXISTS idx_promotion_object ON promotion_candidates(object_id)
    WHERE status NOT IN ('approved', 'rejected');
  ```

  The `UNIQUE` partial index prevents duplicate active proposals for the same object while allowing re-proposal after rejection.

- [ ] **Step 3: Add Pydantic models** to `backend/app/models/schemas.py` (after `MergeReviewSummary` at line 414):

  ```python
  class PromotionCandidate(BaseModel):
      id: str
      notebook_id: str
      object_id: str
      object_type: str
      status: str
      reason: str = ""
      reviewed_by: str = ""
      base_match_id: str = ""
      created_at: str = ""
      # Denormalised fields populated by repo from knowledge_objects:
      payload: dict = Field(default_factory=dict)
      evidence: List[Evidence] = Field(default_factory=list)

  class PromotionApproveResult(BaseModel):
      candidate_id: str
      base_object_id: str
      merged_into: str = ""   # non-empty if deduped into existing base object

  class PromotionRejectRequest(BaseModel):
      reason: str = ""
  ```

- [ ] **Step 4: Implement `propose_promotion(notebook_id, object_id)`** in `sqlite_repository.py`:

  Logic:
  1. `get_notebook(notebook_id)` → KeyError if missing.
  2. SELECT from `knowledge_objects WHERE id=? AND notebook_id=?` → KeyError if missing; store `object_type`.
  3. Check `notebooks.tier` for `notebook_id`; raise `ValueError("cannot propose from a base notebook — use the review gate")` if tier is `'base'`.
  4. Check `promotion_candidates` for an active (non-approved, non-rejected) row for `object_id`; if found, return it (idempotent).
  5. INSERT `(id=promo-{uuid4()[:10]}, notebook_id, object_id, object_type, status='proposed', created_at, updated_at)`.
  6. Return a dict matching `PromotionCandidate`.

- [ ] **Step 5: Implement `list_promotion_queue(status_filter=None)`** — global across all notebooks (the curator sees everything):

  Logic: SELECT * FROM `promotion_candidates` WHERE `status IN ('proposed','under_review')` (override with `status_filter` if given), JOIN `knowledge_objects` to get `payload` + `evidence`. Return `List[PromotionCandidate]`.

- [ ] **Step 6: Implement `approve_promotion(candidate_id)`**:

  Logic (single `_write()` transaction):
  1. Fetch candidate row; raise KeyError if missing; raise ValueError if `status == 'rejected'`.
  2. Idempotency: if `status == 'approved'`, look up already-created base object via `source_candidate_id = candidate_id` in the base notebook's `knowledge_objects`; return `PromotionApproveResult`.
  3. Find the base notebook: SELECT `id FROM notebooks WHERE tier='base' LIMIT 1`; raise ValueError `"no base notebook — mark one with mark_notebook_base() first"` if absent.
  4. Cross-corpus dedup: call `cluster_objects` from `kg_merge.py` against existing base objects of the same `object_type` (fetch their payloads + vectors via `_knowledge_vectors`). If a cluster maps `candidate.object_id` to an existing base object (`len(cluster.members) > 1`), set `base_match_id` on the candidate and merge payloads (combine evidence lists, keep base object's canonical id). Stamp candidate `base_match_id`.
  5. If no dedup match: INSERT a new `knowledge_objects` row in the base notebook with `status='approved'`, `owner=''`, `source_candidate_id=candidate_id`; embed it via `_embed_knowledge`.
  6. UPDATE `promotion_candidates SET status='approved', base_match_id=…, reviewed_by='curator', updated_at=now`.
  7. `_invalidate_unified_cache(base_notebook_id)`.
  8. Return `PromotionApproveResult`.

  **Dedup shortcut for v1:** when vectors are not yet built for the base corpus (cold start), skip the vector-cluster step and use exact `seed_fn` match only (call `kg_merge.seed_concept` / `seed_claim` etc. as appropriate for `object_type`).

- [ ] **Step 7: Implement `reject_promotion(candidate_id, reason="")`**:

  Logic:
  1. Fetch candidate; raise KeyError if missing; raise ValueError if `status == 'approved'`.
  2. UPDATE `status='rejected', reason=reason, reviewed_by='curator', updated_at=now`.
  3. Return updated `PromotionCandidate`.

- [ ] **Step 8: Add base strong-review gate to `store_kg`** (line 1944, the `db.executemany` INSERT):

  Change `'approved'` literal in the INSERT to a variable:

  ```python
  nb_row = db.execute("SELECT tier FROM notebooks WHERE id=?", (notebook_id,)).fetchone()
  auto_status = 'reviewed' if (nb_row and nb_row['tier'] == 'base') else 'approved'
  ```

  Then use `auto_status` instead of the hardcoded `'approved'` string. Objects that are `'reviewed'` ARE in `USABLE_STATUSES` (line 121), so they still surface in retrieval — the gate just signals "curator should confirm before treating as canonical". The curator then uses `update_knowledge` (existing endpoint at `routes.py:312`) to flip `reviewed → approved`.

  Note: `_test_insert_object` (line 2628) bypasses `store_kg` and hardcodes `'approved'` — leave it unchanged (test helper for personal notebooks only).

- [ ] **Step 9: Run tests** (gate: all new tests pass, full suite stays green):

  ```bash
  cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/wave2/backend
  /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_trackF_governance_promotion.py -v
  /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q
  ```

**Gate:** `test_trackF_governance_promotion.py` all pass; full `pytest -q` green; `test_two_tier_federated.py` not broken.

---

## Task 2 — API endpoints

**Files:**
- Modify: `backend/app/api/routes.py`
- Modify: `backend/app/models/schemas.py` (already updated in Task 1)

### Steps

- [ ] **Step 1: Add imports** at the top of `routes.py` (extend the `from app.models.schemas import` block around line 12):

  Add `PromotionCandidate`, `PromotionApproveResult`, `PromotionRejectRequest`.

- [ ] **Step 2: Write test stubs** — add an `import httpx` + `TestClient` smoke test to `test_trackF_governance_promotion.py`:

  ```python
  class TestPromotionRoutes:
      def test_propose_returns_201(self, client, personal_nb, personal_object): ...
      def test_queue_lists_proposed(self, client, proposed_candidate): ...
      def test_approve_returns_200_and_base_object_id(self, client, base_nb, proposed_candidate): ...
      def test_reject_returns_200_and_candidate_with_reason(self, client, proposed_candidate): ...
      def test_propose_unknown_notebook_returns_404(self, client): ...
      def test_propose_base_notebook_returns_400(self, client, base_nb, base_object): ...
  ```

  Use `fastapi.testclient.TestClient(app)` as in `test_unified_kg_api.py`.

- [ ] **Step 3: Add 4 routes** to `routes.py`, after the `review_unified_kg_merges` endpoint (line 633), following the same pattern as `derived-rules` (lines 496-521):

  ```python
  # --- Governance: promotion queue ----------------------------------------

  @router.post(
      "/notebooks/{notebook_id}/knowledge/{knowledge_id}/promote",
      response_model=PromotionCandidate,
      status_code=201,
  )
  def propose_promotion(notebook_id: str, knowledge_id: str) -> PromotionCandidate:
      try:
          return PromotionCandidate(**repository().propose_promotion(notebook_id, knowledge_id))
      except KeyError:
          raise HTTPException(status_code=404, detail="Notebook or knowledge object not found")
      except ValueError as exc:
          raise HTTPException(status_code=400, detail=str(exc))


  @router.get("/promotion-queue", response_model=List[PromotionCandidate])
  def list_promotion_queue(status: str = Query(None)) -> List[PromotionCandidate]:
      return [PromotionCandidate(**c) for c in repository().list_promotion_queue(
          status_filter=status
      )]


  @router.post(
      "/promotion-queue/{candidate_id}/approve",
      response_model=PromotionApproveResult,
  )
  def approve_promotion(candidate_id: str) -> PromotionApproveResult:
      try:
          return PromotionApproveResult(**repository().approve_promotion(candidate_id))
      except KeyError:
          raise HTTPException(status_code=404, detail="Promotion candidate not found")
      except ValueError as exc:
          raise HTTPException(status_code=400, detail=str(exc))


  @router.post(
      "/promotion-queue/{candidate_id}/reject",
      response_model=PromotionCandidate,
  )
  def reject_promotion(candidate_id: str, payload: PromotionRejectRequest) -> PromotionCandidate:
      try:
          return PromotionCandidate(**repository().reject_promotion(
              candidate_id, reason=payload.reason
          ))
      except KeyError:
          raise HTTPException(status_code=404, detail="Promotion candidate not found")
      except ValueError as exc:
          raise HTTPException(status_code=400, detail=str(exc))
  ```

  **API contract (for frontend Task 4):**

  | Method | URL | Request body | Response |
  |---|---|---|---|
  | `POST` | `/api/notebooks/{nb}/knowledge/{ko_id}/promote` | — | `PromotionCandidate` (201) |
  | `GET` | `/api/promotion-queue[?status=proposed]` | — | `PromotionCandidate[]` |
  | `POST` | `/api/promotion-queue/{cand_id}/approve` | — | `PromotionApproveResult` |
  | `POST` | `/api/promotion-queue/{cand_id}/reject` | `{"reason": "…"}` | `PromotionCandidate` |

  `PromotionCandidate` shape returned by the API:
  ```json
  {
    "id": "promo-abc123",
    "notebook_id": "nb-...",
    "object_id": "ko-...",
    "object_type": "claim",
    "status": "proposed",
    "reason": "",
    "reviewed_by": "",
    "base_match_id": "",
    "created_at": "2026-06-10T…",
    "payload": {"name": "…", …},
    "evidence": [{"source_title": "…", …}]
  }
  ```

- [ ] **Step 4: Run route tests**:

  ```bash
  cd /Users/hzf/workspace/silicon_notebook/.claire/worktrees/wave2/backend
  /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_trackF_governance_promotion.py::TestPromotionRoutes -v
  /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q
  ```

  *(Correct path: `/Users/hzf/workspace/silicon_notebook/.claude/worktrees/wave2/backend`)*

**Gate:** `TestPromotionRoutes` all pass; `pytest -q` green.

---

## Task 3 — Base strong-review gate tests & edge cases

**Files:**
- Modify: `backend/tests/test_trackF_governance_promotion.py`

### Steps

This task hardens the `store_kg` gate added in Task 1 Step 8 and covers edge cases missed by the happy path.

- [ ] **Step 1: Add edge-case tests** (add to existing test file):

  ```python
  class TestBaseReviewGateEdgeCases:
      def test_store_kg_base_objects_appear_in_retrieval_as_reviewed(self, repo):
          """Objects stored to base with status='reviewed' must be in USABLE_STATUSES
          and therefore appear in _knowledge_objects queries."""
          ...  # assert status == 'reviewed', assert in list_knowledge(...)

      def test_curator_can_upgrade_reviewed_to_approved(self, repo):
          """update_knowledge(status='approved') on a base reviewed object must succeed."""
          ...  # call update_knowledge with KnowledgeUpdate(status='approved')

      def test_ask_surfaces_base_reviewed_objects(self, repo):
          """ask() on a personal notebook must surface base objects at status='reviewed'.
          Regression guard for USABLE_STATUSES inclusion."""
          ...  # seed base + personal, ask(), assert base object id in anchors or related

      def test_reject_promotion_does_not_affect_personal_retrieval(self, repo):
          """After rejection the personal object must still be retrievable from its
          personal notebook (no side-effects on the personal corpus)."""
          ...

      def test_approve_promotion_object_not_retrievable_from_personal_notebook_ask(self, repo):
          """After promotion approval, the base copy is live. The personal copy remains
          in its personal notebook (personal tier weight unchanged). Ask against the
          personal notebook should see both (via federation), but a base-only ask
          should not surface the personal copy."""
          ...

      def test_double_promotion_is_idempotent(self, repo):
          """propose_promotion() called twice for the same object returns the same
          candidate id without inserting a duplicate row."""
          ...
  ```

- [ ] **Step 2: Run**:

  ```bash
  cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/wave2/backend
  /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_trackF_governance_promotion.py -v
  /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q
  ```

**Gate:** All `TestBaseReviewGateEdgeCases` pass; `pytest -q` green; `test_two_tier_federated.py` not broken.

---

## Task 4 — Frontend promotion queue

**Files:**
- Create: `frontend/app/promotion-queue.ts`
- Modify: `frontend/app/page.tsx`

### Context

The frontend has no test framework for React components. The convention (from `package.json` line 9: `"test": "node --test app/*.test.mjs"`) is:
- Pure TypeScript logic modules (API client, type helpers) → extract to `app/promotion-queue.ts` + test in `app/promotion-queue.test.mjs`.
- React component additions to `page.tsx` → no unit test; gate is `npm run lint` (tsc --noEmit) + `npm run build` pass + manual visual check.

The UI pattern to follow is the `派生规则候选` (derived rules) modal (lines 2917-2952 in `page.tsx`):
- `useState<PromotionCandidate[] | null>` + `useState<boolean>` for open state.
- `openPromoQueue()` async function fetches from `GET /promotion-queue` and sets state.
- `decidePromotion(id, "approve"|"reject")` calls the endpoint and refreshes.
- Modal renders as `<section className="utility-modal" role="dialog">` containing an `<article className="item">` per candidate.
- Actions use `<button className="sort-button">拒绝</button>` and `<button className="new-pill">批准</button>`.

### Steps

- [ ] **Step 1: Create `frontend/app/promotion-queue.ts`** — pure API-client module:

  ```typescript
  export type PromotionCandidate = {
    id: string;
    notebook_id: string;
    object_id: string;
    object_type: string;
    status: string;
    reason: string;
    reviewed_by: string;
    base_match_id: string;
    created_at: string;
    payload: Record<string, unknown>;
    evidence: Array<{
      source_title?: string;
      quoted_span?: string;
      confidence?: number;
      [k: string]: unknown;
    }>;
  };

  export type PromotionApproveResult = {
    candidate_id: string;
    base_object_id: string;
    merged_into: string;
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

  export const fetchPromotionQueue = (status?: string): Promise<PromotionCandidate[]> =>
    apiFetch(`/promotion-queue${status ? `?status=${status}` : ""}`);

  export const proposePromotion = (
    notebookId: string,
    objectId: string
  ): Promise<PromotionCandidate> =>
    apiFetch(`/notebooks/${notebookId}/knowledge/${objectId}/promote`, {
      method: "POST",
    });

  export const approvePromotion = (
    candidateId: string
  ): Promise<PromotionApproveResult> =>
    apiFetch(`/promotion-queue/${encodeURIComponent(candidateId)}/approve`, {
      method: "POST",
    });

  export const rejectPromotion = (
    candidateId: string,
    reason = ""
  ): Promise<PromotionCandidate> =>
    apiFetch(`/promotion-queue/${encodeURIComponent(candidateId)}/reject`, {
      method: "POST",
      body: JSON.stringify({ reason }),
    });
  ```

- [ ] **Step 2: Create `frontend/app/promotion-queue.test.mjs`** — test URL construction (no HTTP calls):

  ```js
  import test from "node:test";
  import assert from "node:assert/strict";

  // Smoke test: module exports the expected functions (no import error = API URLs
  // are syntactically valid; actual HTTP is not exercised in unit tests).
  import {
    fetchPromotionQueue,
    proposePromotion,
    approvePromotion,
    rejectPromotion,
  } from "./promotion-queue.ts";

  test("fetchPromotionQueue is a function", () => {
    assert.strictEqual(typeof fetchPromotionQueue, "function");
  });

  test("proposePromotion is a function", () => {
    assert.strictEqual(typeof proposePromotion, "function");
  });

  test("approvePromotion is a function", () => {
    assert.strictEqual(typeof approvePromotion, "function");
  });

  test("rejectPromotion is a function", () => {
    assert.strictEqual(typeof rejectPromotion, "function");
  });
  ```

- [ ] **Step 3: Add state variables** to `page.tsx` (near the `derivedRules` state at line 802):

  ```tsx
  // Promotion queue modal
  const [promoQueue, setPromoQueue] = useState<PromotionCandidate[] | null>(null);
  const [promoOpen, setPromoOpen] = useState(false);
  const [promoBusy, setPromoBusy] = useState(false);
  ```

  Add `PromotionCandidate` and `PromotionApproveResult` type imports from `./promotion-queue`.

- [ ] **Step 4: Add `openPromoQueue()` and `decidePromotion()` functions** in `page.tsx` (near `openDerivedRules` at line 1829):

  ```tsx
  async function openPromoQueue() {
    const queue = await fetchPromotionQueue();
    setPromoQueue(queue);
    setPromoOpen(true);
  }

  async function decidePromotion(candidateId: string, decision: "approve" | "reject", reason = "") {
    if (!currentNotebookId) return;
    setPromoBusy(true);
    try {
      if (decision === "approve") {
        const result = await approvePromotion(candidateId);
        const merged = result.merged_into ? `（与 ${result.merged_into.slice(0, 8)} 合并去重）` : "";
        setToast(`晋升已批准${merged}，节点已加入基准语料`);
      } else {
        await rejectPromotion(candidateId, reason);
        setToast("晋升已拒绝，个人 KG 节点保持不变");
      }
      // Refresh queue
      const queue = await fetchPromotionQueue();
      setPromoQueue(queue);
      // Refresh base notebook knowledge if we have it loaded
      await loadNotebookCollection();
    } finally {
      setPromoBusy(false);
    }
  }
  ```

- [ ] **Step 5: Add a "晋升队列" button** in the toolbar/sidebar next to the "派生规则" button (locate line ~2098 where `LayoutDashboard` icon is used for derived rules) and add the modal markup after the `derivedOpen` modal block (after line 2952):

  Toolbar button (mirror the derived-rules button style, exact CSS class unknown — use `ghost-button` as used at line 2775):

  ```tsx
  <button className="ghost-button" onClick={openPromoQueue}>
    晋升队列
  </button>
  ```

  Modal (after line 2952, after the `derivedOpen` modal's closing `</section>`):

  ```tsx
  {promoOpen && (
    <section
      className="utility-modal"
      role="dialog"
      aria-modal="true"
      onClick={(event) => { if (event.currentTarget === event.target) setPromoOpen(false); }}
    >
      <div className="utility-modal-card">
        <div className="source-modal-header">
          <div>
            <h2>晋升队列</h2>
            <p>个人 KG 节点申请晋升到基准语料。批准后会执行去重并加入基准库。</p>
          </div>
          <button className="icon-button" onClick={() => setPromoOpen(false)} title="Close">×</button>
        </div>
        <div className="source-detail-body">
          {(promoQueue ?? []).length === 0 ? (
            <p className="tool-hint">暂无待审晋升请求。</p>
          ) : (
            <div className="stack">
              {(promoQueue ?? []).map((cand) => (
                <article className="item" key={cand.id}>
                  <div className="tag-row">
                    <span className="tag">{cand.status}</span>
                    <span className="tag">{cand.object_type}</span>
                    {cand.base_match_id && (
                      <span className="tag conflict">去重匹配: {cand.base_match_id.slice(0, 10)}</span>
                    )}
                  </div>
                  <h3>{String((cand.payload as any).name ?? (cand.payload as any).title ?? cand.object_id)}</h3>
                  <p className="tool-hint">来源笔记本: {cand.notebook_id.slice(0, 10)}</p>
                  {cand.evidence.length > 0 && (
                    <p><strong>证据：</strong>{cand.evidence[0].quoted_span ?? ""}</p>
                  )}
                  {cand.base_match_id && (
                    <p className="conflict-note">基准库中已有相似节点 — 批准后将合并去重。</p>
                  )}
                  {(cand.status === "proposed" || cand.status === "under_review") && (
                    <div className="modal-actions">
                      <button
                        className="sort-button"
                        disabled={promoBusy}
                        onClick={() => decidePromotion(cand.id, "reject").catch(reportError)}
                      >
                        拒绝
                      </button>
                      <button
                        className="new-pill"
                        disabled={promoBusy}
                        onClick={() => decidePromotion(cand.id, "approve").catch(reportError)}
                      >
                        批准晋升
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

- [ ] **Step 6: Also add "提交晋升" (propose) button** on individual knowledge items in the knowledge-list view. Locate the existing knowledge item action buttons (they appear near the status dropdown/edit controls). Add a propose button only when the current notebook `tier === 'personal'`:

  ```tsx
  {currentNotebook?.tier === "personal" && item.status !== "deprecated" && (
    <button
      className="icon-button subtle-icon"
      title="提交晋升到基准库"
      onClick={() =>
        proposePromotion(currentNotebookId, item.id)
          .then(() => setToast("已提交晋升请求"))
          .catch(reportError)
      }
    >
      ↑
    </button>
  )}
  ```

  Note: `currentNotebook` is already in state as the `NotebookSummary` type; add `tier?: string` to the existing frontend `NotebookSummary` type definition (line 24-37 of `page.tsx`).

- [ ] **Step 7: Run frontend checks**:

  ```bash
  cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/wave2/frontend
  npm test            # node --test app/*.test.mjs  — promotion-queue.test.mjs must pass
  npm run lint        # tsc --noEmit               — zero type errors
  npm run build       # next build                 — must succeed
  ```

**Gate:** `npm test` green (all `.test.mjs` including new `promotion-queue.test.mjs`); `npm run lint` zero errors; `npm run build` succeeds.

---

## Final Phase Gate

The track is complete when ALL of the following are true:

**Backend E2E scenario:**
- [ ] Create a personal notebook, store a KG object, call `propose_promotion` → candidate appears in `list_promotion_queue()` with `status='proposed'`.
- [ ] Call `approve_promotion(candidate_id)` → `base_object_id` is non-empty → SELECT from `knowledge_objects WHERE notebook_id=<base_nb>` finds the row → `status='approved'` → `tier` of its notebook is `'base'`.
- [ ] `federated_retrieve` from the personal notebook returns the promoted base object.
- [ ] Call `reject_promotion(candidate_id, reason="not canonical")` for a different candidate → `promotion_candidates.status='rejected'` → personal object's `status` is unchanged → `ask()` on a base-only query does NOT include the rejected personal object.
- [ ] `store_kg` into a base notebook → objects have `status='reviewed'` → they appear in `list_knowledge` results (USABLE_STATUSES includes `'reviewed'`) → `update_knowledge(status='approved')` succeeds.

**Test suite:**
- [ ] `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q` — **all green, zero regressions**.
- [ ] `test_two_tier_federated.py` — all 5 task suites still pass.
- [ ] `test_knowledge_governance_boundaries.py` — all 5 tests still pass.

**Frontend:**
- [ ] `cd frontend && npm test` — all `.test.mjs` pass.
- [ ] `npm run lint` — zero TypeScript errors.
- [ ] `npm run build` — exits 0.
- [ ] Manual check: open the app, navigate to a personal notebook, click "提交晋升" on a knowledge item → toast "已提交晋升请求"; open "晋升队列" → candidate appears; click "批准晋升" → toast confirms; switch to base notebook → item visible in knowledge list.

---

## Commit sequence

1. After Task 1 + 2 backend green: `git commit -m "feat(governance): promotion state machine + API (Track F T1-T2)"`
2. After Task 3 edge cases green: `git commit -m "test(governance): base strong-review gate edge cases (Track F T3)"`
3. After Task 4 frontend green: `git commit -m "feat(governance): promotion queue UI + propose button (Track F T4)"`
