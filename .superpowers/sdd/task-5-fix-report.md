# Task 5 fix report: index guarded anchor membership

## Delivery

- Implementation commit: `b91b013b263d1938dbe06c83e7bc37835f7a38cf`
- The frozen `backend/tests/fixtures/repository_v9/baseline.db` was not changed.
- Full `scripts/check.sh` was intentionally not run; the controller owns that
  final gate.

## RED → GREEN

RED was observed before production edits with:

```text
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest \
  backend/tests/test_knowhow_store.py -q -k normalized_anchor_index
```

It failed as intended. The plan was:

```text
SEARCH r USING INDEX idx_knowhow_rows_table (table_id=?)
SEARCH c USING INDEX sqlite_autoindex_knowhow_cells_2 (row_id=? AND column_id=?)
```

and did not use `idx_knowhow_cells_column_normalized_anchor_row`.

GREEN replaces the leading-wildcard prefilter with equality on the shared
SQLite ECMAScript-trim expression and adds migration 21's expression index:
`(column_id, JS-trim(content_md), row_id)`. The query-plan regression now sees
the normalized anchor index and no table scan. SQL normalization is checked
against Python/JS trimming for every included code point and preserves the
excluded U+001C–U+001F and U+0085 edge cases.

## Verification

```text
pytest focused store/schema/migrator/legacy/verifier/docs/facade set: 295 passed
python -m py_compile anchor_normalization.py knowhow_store.py migrations.py \
  sqlite_repository.py verify_repository_snapshot.py: passed
scripts/verify_repository_snapshot.py --database .../repository_v9/baseline.db \
  --storage-dir .../repository_v9/storage: PASS schema=v9 → final v21
git diff --check: passed
```

The focused suite retains the existing exotic-whitespace joiner, blank
singleton, anchor-designation, foreign-table, parallel-length mismatch, and
subset-write coverage. It also adds the v20→v21 snapshot-verifier migration
path.

## Generated and compatibility artifacts

- Regenerated the schema golden through its documented
  `UPDATE_SCHEMA_GOLDEN=1` test path.
- Ran `scripts/generate_repository_contract_fixtures.py`; it refreshed only
  the living v9 expected snapshot and its manifest for this schema change.
- Updated exact verifier manifests for every supported source version through
  v20, including the direct `(20, 21)` hop.
- Updated `README.md`, `README_zh.md`, `AGENTS.md`, and `architecture.md` for
  schema v21 and the index-backed interactive reformat membership contract.
