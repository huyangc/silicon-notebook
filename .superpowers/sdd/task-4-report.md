# Task 4 report — typed Memory links and Knowhow health filters

## RED

Command:

```bash
cd frontend && node --test app/memory-navigation.test.mjs app/knowhow-model.test.mjs
```

Observed expected failures before implementation:

- `filterKnowhowTables` was not exported.
- Existing parsed Memory hashes lacked `filterNotebookId`, `status`, and `itemId`.
- `memoryHash(null, target)` still returned only `#memory`.

The mapping regression test was also independently run RED after temporarily
removing the new map fields: `fetchKnowhowTables maps health summaries with
safe defaults` failed because `projectionPending`, `projectionFailed`,
`staleCodeCount`, and `lastActivityAt` were absent.

## GREEN

Commands:

```bash
cd frontend && node --test app/memory-navigation.test.mjs app/knowhow-model.test.mjs
cd frontend && npx tsc --noEmit
```

Output:

- Node test suites: 101 passed, 0 failed.
- TypeScript: exit 0, no diagnostics.

No full gate was run, as requested.

## Files changed

- `frontend/app/memory-model.ts`
- `frontend/app/memory-navigation.test.mjs`
- `frontend/app/memory-panel.tsx`
- `frontend/app/knowhow-model.ts`
- `frontend/app/knowhow-model.test.mjs`
- `frontend/app/knowhow-panel.tsx`

## Compatibility evidence

- `memoryHash(null)` remains exactly `#memory`.
- `memoryHash("nb-1")` remains exactly `#notebook=nb-1&tab=memory`.
- Notebook Memory parsing returns null target fields; `parseWorkspaceHash()`
  explicitly rejects all `memory` hashes, keeping the parsers mutually
  exclusive.
- Deep-linked Memory items only add their id to `expandedIds`; navigation
  never changes `editingId`.
- The Knowhow filter only narrows the existing table-card list and leaves the
  destination’s existing `canEdit` controls and read-only behavior unchanged.

## Self-review

- Confirmed valid Memory statuses are allow-listed and unknown values become
  `null`.
- Confirmed optional Knowhow wire fields map to zero/empty defaults, including
  old responses without the new summary fields.
- Confirmed a non-empty unfiltered table list with no health matches retains
  the selector and shows `当前筛选下没有表`.
- `git diff --check` completed without whitespace errors.

## Concerns

None. Wiring the new optional panel props from content-overview destinations is
intentionally outside this task’s assigned files.

## Review fixes

### RED

```bash
cd frontend && npx vitest run app/content-navigation.component.test.tsx
```

Observed both intended failures before the production fix:

- Rerendering `KnowhowPanel` from `projection_pending` to
  `projection_failed` left the selector at `projection_pending`.
- Rerendering global `MemoryPanel` after stale origin/search/edit/expansion
  state left the follow-up request with `origin` and `query`, rather than the
  new navigation target’s clean read-only destination.

### GREEN

```bash
cd frontend && node --test app/memory-navigation.test.mjs app/knowhow-model.test.mjs
cd frontend && npx vitest run app/content-navigation.component.test.tsx
cd frontend && npx tsc --noEmit
git diff --check
```

Observed:

- Node model/hash suites: 101 passed, 0 failed.
- Component regressions: 2 passed, 0 failed. They prove the Knowhow target
  moves pending → failed → all, and that a global Memory target change clears
  stale origin/search/page/edit/previous deep-link expansion before opening
  the new target read-only.
- TypeScript and whitespace checks exited 0.

The component test emits pre-existing jsdom warnings for the production
`<style jsx global>` attributes; no assertions or behavior depend on them.
