# Task 1 report: typed content overview and fixed projections

## Implementation

- Added typed Memory and Knowhow overview response models plus the additive
  Knowhow table health fields.
- Added consumer-specific Memory and Knowhow projection ports.
- Added the owner-and-notebook scoped Memory projection. It uses two SELECT
  statements, bounds recency to one through three rows, counts all lifecycle
  states in `total`, and returns only confirmed/candidate recent rows.
- Replaced the Knowhow table summary read with a fixed four-query health-input
  projection: table rows, per-table row/status/activity aggregation, cell
  activity, and code-attachment/current-cell pairs. `pending` and `syncing`
  are intentionally grouped in `projection_pending`.
- Added `ContentOverviewService`, which derives canonical code freshness with
  `cell_content_hash`, aggregates health totals, and selects the three most
  recently active tables.
- Preserved the existing `list_knowhow_tables` compatibility fields
  (`mutation_seq`, `hidden_source_id`, and `created_by`) in the first health
  query while delegating that method to the new projection. This does not add a
  statement family or change the four-query bound.

## Files

- `backend/app/models/schemas.py`
- `backend/app/repositories/ports.py`
- `backend/app/repositories/sqlite/memory_store.py`
- `backend/app/repositories/sqlite/knowhow_store.py`
- `backend/app/services/content_overview.py`
- `backend/tests/test_content_overview_service.py`
- `backend/tests/test_knowhow_store.py`

## RED evidence

Command:

```text
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider -n0 backend/tests/test_content_overview_service.py -q
```

Output:

```text
ERROR collecting tests/test_content_overview_service.py
ModuleNotFoundError: No module named 'app.services.content_overview'
1 error in 0.19s
```

This was the expected collection failure: the new service module (and its
dependent typed response surface) did not exist yet.

## GREEN evidence

Command:

```text
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider -n0 backend/tests/test_content_overview_service.py backend/tests/test_knowhow_store.py -q
```

Output:

```text
130 passed in 7.30s
```

## Self-review

- Service tests prove the viewer id is passed to Memory, image-only Markdown
  does not create a false stale-code result, activity ordering is descending,
  and all empty sections remain typed.
- SQLite Memory tests prove notebook/user isolation, lifecycle totals, active
  recent-only results, the three-row bound, and exactly two Memory SELECTs.
- Knowhow store tests prove 25 tables use exactly four SELECTs and that one
  `syncing` row counts as pending while one failed row counts as failed.
- `git diff --check` passed with no whitespace errors.

## Concerns

None for this task slice. The repository-wide gate was intentionally not run,
per the task brief; the focused acceptance command above is green.

## Review fixes

### Files

- `backend/app/repositories/ports.py`
- `backend/app/services/content_overview.py`
- `backend/tests/test_content_overview_service.py`

### Tests

Requested command:

```text
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider -n0 backend/tests/test_content_overview_service.py backend/tests/test_repository_callers_static.py backend/tests/test_repository_surface_manifest.py backend/tests/test_repository_dependency_contract.py -q
```

Output:

```text
no tests ran in 0.00s
ERROR: file or directory not found: backend/tests/test_repository_callers_static.py
```

This worktree contains the current contract test under
`test_repository_surface_contract.py`; the requested static and manifest files
are absent. The available covering command was therefore:

```text
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider -n0 backend/tests/test_content_overview_service.py backend/tests/test_repository_surface_contract.py backend/tests/test_repository_dependency_contract.py -q
```

Output:

```text
.....................                                                    [100%]
21 passed in 5.33s
```

`git diff --check` and `git diff --check HEAD` both completed with no output.

### Self-review

- `ReportSourceQueryPort` remains in its original final position; both new
  Content Overview protocols now follow it at true EOF without signature edits.
- The service now removes Markdown image tokens before computing attachment
  freshness, so the added `cell_content_hash("new")` attachment stays fresh
  for `![plot](asset://img)\nnew`; the deliberately stale `old` attachment
  still makes `stale_code_count == 1`.

### Canonical hash correction

The review-fix image-stripping regex approximation was rejected: it disagreed
with the sole canonical freshness rule, `cell_content_hash`, whose image
normalization retains a machine-safe placeholder. The fresh image-bearing
attachment now stores `cell_content_hash("![plot](asset://img)\nnew")` against
identical current Markdown, while the separate `old` versus current attachment
remains deliberately stale (`stale_code_count == 1`).

Covering command:

```text
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider -n0 backend/tests/test_content_overview_service.py backend/tests/test_repository_surface_contract.py backend/tests/test_repository_dependency_contract.py -q
```

Output:

```text
.....................                                                    [100%]
21 passed in 5.32s
```
