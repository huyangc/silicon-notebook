# Task 1 — Complete Historical Log Coverage

## Status

Complete. Historical diagnostic readers now discover legacy, date-rotated, gzip-compressed, and one-level per-user channel logs through one bounded, stdlib-only reader.

## Changes

- Added `scripts/diag_common.py` with the required public `ChannelRecords`, `discover_channel_files`, `iter_jsonl_file`, `read_channel`, and `normalize_http_path` interfaces. It discovers legacy, daily, gzip, and one-level per-user logs; deduplicates stable records; counts malformed input; applies time windows/retention limits; enforces 64 MiB/deadline bounds; and removes query strings plus configured identifier-shaped path segments.
- Refactored `scripts/diag_slow.py` to use the shared reader for requests, events, and LLM logs. Section output now includes scan metadata; `_iter_jsonl` is a compatibility wrapper over the shared iterator.
- Refactored `scripts/diag_open_latency.py` to aggregate all historical request layouts through the shared reader and path normalizer.
- Refactored `scripts/diag.py` latency input to use the events-channel reader, retaining `--log` as an explicit path hint before applying `--last`.
- Added layout/deduplication/window/malformed/privacy coverage in `backend/tests/test_diag_common.py`, plus the gzip-only latency fixture in `backend/tests/test_diag_unified.py`.

## TDD evidence

### RED

Before `scripts/diag_common.py` existed, I ran:

`PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider backend/tests/test_diag_common.py backend/tests/test_diag_unified.py -q`

Result: `4 failed, 11 passed in 2.44s`.

- Three shared-reader tests failed with `FileNotFoundError` for the absent `scripts/diag_common.py`.
- The gzip-only latency test failed because output contained no `score` stage.

### GREEN

After the minimal shared-reader implementation and consumer refactors, the same focused command passed: `15 passed in 3.96s`.

## Verification

`PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider backend/tests/test_diag_common.py backend/tests/test_diag_unified.py backend/tests/test_event_logging.py backend/tests/test_debug_logs_days.py -q`

Result: `34 passed in 5.95s`.

`PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider backend/tests/test_repository_dependency_contract.py -q`

Result: `12 passed in 3.81s`.

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m py_compile scripts/diag_common.py scripts/diag_slow.py scripts/diag_open_latency.py scripts/diag.py` and `git diff --check` both passed with no output.

Manual daily-only fixture:

`/opt/homebrew/Caskroom/miniconda/base/bin/python scripts/diag.py slow --local <fixture> --since 999999`

The result included `日志 files=1 matched=1 ... retained=1` and `窗口内请求总数 1`; the rotated `requests-2026-07-21.jsonl` was not reported as zero requests.

## Self-review

- Confirmed all runtime historical-reader call sites in the three diagnostic scripts now call `diag_common.read_channel`; the retained `_iter_jsonl` wrapper is compatibility-only.
- Confirmed offline imports remain stdlib-only and the repository host safety/read-only contract test passes.
- Confirmed no product documentation or later-task files were changed.

## Concerns

None.

## Review remediation (post-commit)

The Task 1 review identified three production-hardening gaps. The follow-up
keeps the same stdlib/offline boundary and changes only the diagnostic reader,
slow report, and focused regression tests.

### Changes

- `normalize_http_path()` now structurally redacts the segment after sensitive
  route markers such as `shared`, `share`, `token`, `auth`, and `session`, and
  also redacts token-prefixed opaque segments. Thus `/shared/shr-opaque...`
  becomes `/shared/{token}` even when the token has no digits.
- `iter_jsonl_file()` now reads binary input in bounded chunks, checks the
  monotonic deadline before every read and before parsing, and sends an
  internal truncation sentinel without allocating or JSON-parsing an oversized
  plain or gzip JSONL line. `read_channel()` passes each remaining decoded-byte
  budget into that iterator and stops on the sentinel.
- `diag_slow.report_requests()` caps request-path aggregation and Top-15
  rendering within a 32 KiB default report envelope. It preserves the most
  useful high-latency rows and writes `output_truncated=True` plus the omitted
  row count and byte budget when it has to omit rows.
- Added regression coverage for opaque share-token redaction, gzip oversized
  lines, deadline-before-parse behavior, and large distinct-path report output.

### TDD RED

After adding the regression tests and before changing production code, I ran:

`PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider backend/tests/test_diag_common.py backend/tests/test_diag_slow.py backend/tests/test_diag_unified.py -q`

Result: `4 failed, 15 passed in 2.39s`.

- Opaque `/shared/shr-...` output retained the token.
- A gzip line exceeding the 128-byte hard bound was fully passed to JSON
  parsing.
- An expired deadline still allowed JSON parsing.
- The requested report-envelope constant/cap did not exist.

### TDD GREEN

After the minimal bounded streaming/redaction/output-cap implementation, I
reran the same focused command:

`19 passed in 2.24s`.

### Follow-up verification

`PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -p no:cacheprovider backend/tests/test_diag_common.py backend/tests/test_diag_slow.py backend/tests/test_diag_unified.py backend/tests/test_event_logging.py backend/tests/test_debug_logs_days.py backend/tests/test_repository_dependency_contract.py -q`

Result: `50 passed in 5.00s`.

`/opt/homebrew/Caskroom/miniconda/base/bin/python -m py_compile scripts/diag_common.py scripts/diag_slow.py scripts/diag_open_latency.py scripts/diag.py` and `git diff --check` both passed with no output.
