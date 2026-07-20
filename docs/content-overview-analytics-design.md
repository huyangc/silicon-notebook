# Content Overview Analytics and Visible Source Semantics

Status: approved

Date: 2026-07-20

Target branch: `codex/content-overview-analytics`

## 1. Purpose

The repository currently stores user-imported sources and internal Memory/Knowhow
projection sources in the same physical `sources` table. Some user-facing counts
therefore expose implementation rows as if they were imported files. At the same
time, the existing notebook analytics view does not show Memory and Knowhow as
separate content assets.

This specialty repays both parts of that historical debt:

1. Every user-facing "source" count means user-imported sources only.
2. Memory and Knowhow are visible as separate cards in the existing analytics
   view.
3. Full browsing and editing continue to use the existing Memory and Knowhow
   pages; the analytics view does not duplicate those features.

## 2. Product Boundaries

- The notebook source rail and the four workspace tabs remain unchanged.
- The existing analytics view gains a "content assets" section containing one
  Memory card and one Knowhow card.
- The Memory card is always private to the current authenticated user.
- Knowhow statistics follow notebook read access. Read-only members can inspect
  the card and navigate to existing content, but cannot edit it.
- Admin cross-user Memory visibility and governance are deliberately deferred to
  a later design.
- This work does not change LLM, KG-build timeout, circuit-breaker, projection,
  index, or notebook-copy scheduling behavior.

## 3. API and Architecture

Add a viewer-aware read endpoint:

```http
GET /notebooks/{notebook_id}/analytics/content-overview
```

The endpoint requires notebook read access and returns:

```json
{
  "memory": {
    "total": 0,
    "confirmed": 0,
    "candidate": 0,
    "recent": [
      {
        "id": "memory-id",
        "title": "Memory title",
        "status": "confirmed",
        "updated_at": "2026-07-20T12:00:00+00:00"
      }
    ]
  },
  "knowhow": {
    "table_count": 0,
    "row_count": 0,
    "projection_pending": 0,
    "projection_failed": 0,
    "stale_code_count": 0,
    "recent_tables": [
      {
        "id": "table-id",
        "title": "Knowhow table",
        "row_count": 0,
        "last_activity_at": "2026-07-20T12:00:00+00:00"
      }
    ]
  }
}
```

The existing `GET /notebooks/{id}/analytics` contract remains unchanged. It is
not extended with viewer-private Memory data because that would mix notebook-wide
analytics with authentication-dependent state and complicate its existing cache
and repository contract.

The route owns authentication and orchestration only. Stores own product SQL,
and an application service/query component assembles the typed response through
consumer-specific repository ports. The feature must not add raw product SQL to
the route, facade, or frontend.

No schema migration is planned. A migration is allowed only if query-plan
evidence shows that the fixed aggregation queries cannot use the existing
indexes.

## 4. Metric Semantics

### 4.1 Memory

All Memory queries are constrained by both the requested notebook and the
current authenticated user's id.

- `total`: all Memory rows owned by the current user in the notebook, including
  rejected and deprecated lifecycle history.
- `confirmed`: rows whose current status is `confirmed`.
- `candidate`: rows whose current status is `candidate`.
- `recent`: the three most recently updated active rows, restricted to
  `confirmed` and `candidate`.

Recent rows expose only id, title, status, and update time. They do not expose
Memory bodies in the analytics response. Rejected and deprecated entries remain
browsable on the existing Memory page but do not receive dedicated dashboard
metrics.

### 4.2 Knowhow

- `table_count`: all Knowhow tables in the notebook.
- `row_count`: the sum of rows across those tables.
- `projection_pending`: rows whose persisted `projection_status` is `pending`
  or `syncing`. The existing projector uses `pending` for queued work and
  `syncing` while processing, so the user-facing pending metric deliberately
  groups both non-terminal states.
- `projection_failed`: rows whose projection is failed.
- `stale_code_count`: cells with a code attachment whose saved
  `cell_content_hash` differs from the hash of the cell's current image-stripped
  text.
- `recent_tables`: the three most recently active tables, with id, title, row
  count, and derived `last_activity_at`.

`last_activity_at` is the maximum relevant update time across the table metadata,
its rows/cells, and code attachments. `knowhow_tables.updated_at` alone is not a
valid recency source because normal cell writes update the row/cell and table
mutation sequence without necessarily updating the table timestamp.

The stale-code calculation must reuse the canonical Knowhow freshness rule.
Neither the endpoint nor the frontend may implement a second approximation.
The existing Knowhow table-summary response gains additive per-table pending,
failed, stale-code, and `last_activity_at` fields. The content-overview query
and the existing Knowhow table list consume the same batched health projection,
so analytics totals and destination filters cannot drift.

### 4.3 Source Counts

User-facing source counts include user-imported files only and exclude source
rows whose `source_type` is `memory` or `knowhow`.

Specifically:

- Share preview `source_count` uses the visible imported-source count.
- Share preview physical `size.sources` remains unchanged because notebook copy
  thresholds and storage calculations need the true physical row count.
- Notebook summary/workspace `kg_pending_sources` and analytics/index-status
  `pending_sources` expose the visible imported-source count.
- User-visible scale-index `unindexed_sources` exposes the visible imported
  source delta. A separate `has_unindexed_content` boolean preserves the
  update-versus-rebuild action decision when only derived content is pending,
  without exposing that derived-row count as a source count.
- Running KG job progress keeps its physical work-unit totals but labels them as
  content items rather than sources, because a job may contain imported and
  derived work together.
- Internal scheduling and readiness counters keep their current physical
  semantics. If an internal counter is currently reused by the UI, expose a
  separate visible counter rather than changing the coordinator's meaning.

## 5. Query and Failure Behavior

- Store methods use a bounded, constant number of aggregate/batch queries.
- Data growth must not introduce one query per Memory, table, row, or cell.
- Opening analytics starts existing analytics, index status, and content
  overview reads in parallel.
- Failure of the content-overview request marks only the Memory and Knowhow cards
  unavailable. Existing notebook analytics and index status continue to render.
- A missing or inaccessible notebook follows the repository's existing uniform
  not-found behavior.
- Empty notebooks return zero metrics and empty recent lists, not missing
  sections.

## 6. Frontend Behavior

The existing analytics view adds a "content assets" section:

- Desktop: Memory and Knowhow cards appear side by side.
- Narrow layouts: cards stack vertically.
- Cards contain metrics, recent items, and navigation only; they never edit
  content directly.

Memory navigation:

- `confirmed` opens the existing global Memory page filtered to the current
  notebook and `confirmed`.
- `candidate` opens it filtered to the current notebook and `candidate`.
- A recent row opens the existing Memory detail.
- "View all" opens the existing Memory page filtered to the current notebook.

Knowhow navigation:

- A recent table opens the existing Knowhow table.
- Pending, failed, and stale-code metrics open the existing Knowhow page with the
  corresponding filter.
- If an existing page lacks one of those filters, this change adds the filter to
  that page instead of adding a list or editor to analytics.
- The filter is table-level: it lists tables containing at least one matching
  row or code attachment, and each result remains the existing table card that
  opens the existing grid/editor.

The page-level navigation owner extends the existing Memory hash contract with
optional notebook, status, and item targets, and passes an explicit Knowhow
table/health-filter target into the existing Knowhow panel. Destination panels
remain the single owners of browsing and editing state; analytics cards do not
retain or clone destination data. Memory deep links restore the global Memory
page with its existing notebook/status filters, while a recent-item target opens
the existing Memory detail.

Read-only users see navigation but encounter the same edit restrictions already
enforced by the destination pages.

## 7. Verification

Backend coverage must prove:

- Memory isolation by current user and notebook for owners, members, and admins.
- Accurate lifecycle counts, recent ordering, and three-row bounds.
- Accurate Knowhow table, row, projection, and stale-code counts.
- A bounded, constant query count.
- Visible share-preview source counts alongside unchanged physical size counts.
- Visible notebook-summary, KG-pending, and scale-index-delta source counts
  excluding derived sources while internal counts retain their existing
  behavior.
- Empty, inaccessible, and not-found behavior.

Frontend coverage must prove:

- Normal, empty, loading-failure, and read-only card states.
- Metric and recent-item navigation contracts.
- Existing analytics still renders when content overview fails.
- No new Memory/Knowhow surface appears in the source rail or workspace tabs.

Completion gates:

- `PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python scripts/check.sh`
  passes.
- The local warm full gate remains at or below 60 seconds.
- `cd frontend && npm run build` passes as part of the full gate.
- Ubuntu GitHub Actions is green; its duration is recorded but is not a hard
  merge gate.
- `README.md`, `README_zh.md`, and `AGENTS.md` are synchronized.
- The specialty ships as one pull request.
- After the PR is created, an independent subagent reviews the exact pushed head.
  Critical and Important findings are resolved before final handoff.

## 8. Explicit Non-Goals

- Admin cross-user Memory analytics.
- A new Memory or Knowhow browser/editor.
- Editing from analytics cards.
- Schema changes without query-plan evidence.
- Changes to physical source accounting used by copy/storage or background
  scheduling.
- LLM/KG build resilience, timeout, or circuit-breaker work.
