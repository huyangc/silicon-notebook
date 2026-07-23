# Production DFX Incident Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a production-safe DFX suite that identifies a currently stuck backend operation from one sanitized, copyable command while repairing coverage across all existing diagnostic commands.

**Architecture:** Add a bounded in-process diagnostics runtime for active requests, jobs, SQLite work, lock contention, concurrency gates, heartbeat snapshots, and non-terminating `SIGUSR1` stacks. Keep the operator tools pure-standard-library and application-import-free: shared log discovery feeds the existing historical commands, while separate DB, Linux-process, diagnosis-rule, and report modules compose `diag.py incident`.

**Tech Stack:** Python standard library (`sqlite3`, `gzip`, `faulthandler`, `signal`, `threading`, `contextvars`, `urllib`, Linux `/proc`), FastAPI/Starlette middleware, pytest, existing shell/Next.js verification.

## Global Constraints

- Production target is Ubuntu 24.04 started from the repository with `npm run start`; the backend remains one Uvicorn worker.
- The operator can SSH to the host but must be able to copy the default result as one text block no larger than approximately 32 KiB.
- `incident`, `slow`, `latency`, `open`, and `db` use only the Python standard library and never import `app`; only `base-recall` may lazily import the application.
- Diagnostics may mutate only their own bounded `.local/diagnostics/` artifacts. They never write application data, execute a delete, checkpoint, vacuum, analyze, reindex, migration, restart, or terminating signal.
- `SIGUSR1` is registered as a non-terminating all-thread Python stack dump. No local-variable values are captured.
- Runtime snapshots and reports contain metadata only. Never persist or print request bodies, source text, Ask questions/answers, prompts, model messages, Memory/Knowhow content, SQL parameters, authorization data, cookies, tokens, secrets, or raw user-controlled filenames.
- Exact opaque notebook identifiers may exist only in the machine-local runtime snapshot for read-only correlation. Copyable output must pseudonymize them consistently.
- Normal-path instrumentation is best-effort and exception-safe. It must not acquire the SQLite write lock, change transaction semantics, or persist one event per SQL statement.
- Snapshot interval is 2 seconds; default incident deadline is 10 seconds; SQLite busy timeout for diagnostic probes is at most 1 second.
- Existing standalone scripts remain runnable, and bare `python3 scripts/diag.py` continues to mean `slow`.
- Do not add a diagnostics UI or frontend API. Full-stack parity is not applicable to this internal infrastructure work.
- Update `README.md`, `README_zh.md`, `AGENTS.md`, and `scripts/README.md` together. Do not update `fangan_done.md` because this is not a completed `silicon_notebook_fangan.md` product feature.
- Final verification must include `bash scripts/check.sh` and `cd frontend && npm run build`.

---

## File Map

### New files

- `scripts/diag_common.py`: log-layout discovery, bounded JSONL/gzip reads, deduplication, path/identifier redaction, and output budgeting.
- `scripts/diag_db.py`: read-only SQLite/WAL/table/FK-index/query-plan evidence and the standalone `db` command.
- `scripts/diag_process.py`: Linux `/proc`, PID resolution, liveness probes, runtime-snapshot validation, `SIGUSR1` capture, and faulthandler-dump parsing.
- `scripts/diag_rules.py`: deterministic evidence-to-finding scoring and copy-safe report rendering.
- `scripts/diag_incident.py`: bounded orchestration of process, runtime, logs, DB evidence, rules, and stdout output.
- `backend/app/core/diagnostics_runtime.py`: process-local registries, context correlation, atomic heartbeat snapshots, SQL metadata normalization, and faulthandler lifecycle.
- `backend/tests/test_diag_common.py`: historical log-layout and sanitization contracts.
- `backend/tests/test_diagnostics_runtime.py`: registry, atomic snapshot, signal, privacy, and failure-isolation contracts.
- `backend/tests/test_diag_db.py`: read-only DB/WAL/FK-index/plan analyzer contracts.
- `backend/tests/test_diag_process.py`: PID, `/proc`, liveness, snapshot, and signal collection contracts.
- `backend/tests/test_diag_incident.py`: diagnosis ranking, output limit, degradation, and synthetic delete-contention report.

### Modified files

- `scripts/diag.py:1-185`: add `incident`, `open`, and `db`; make `latency` use the shared reader.
- `scripts/diag_slow.py:56-71,157-353,911-934`: use shared legacy/daily/gzip/per-user log discovery and expose malformed/retained counts.
- `scripts/diag_open_latency.py:34-50,200-225`: use shared request-log discovery while preserving the standalone command.
- `backend/app/main.py:38-51,100-109,148-188`: diagnostics lifecycle, concurrency provider, and active-request middleware scope.
- `backend/app/repositories/sqlite/database.py:12-128`: cursor-level SQL observation and re-entrant write-lock holder/waiter instrumentation.
- `backend/app/services/background_jobs.py:37-65`: active/recent job lifecycle observation without changing `submit()`'s caller contract.
- `backend/app/services/notebook_catalog.py:380-391`: explicit notebook-delete DB and filesystem phases.
- `backend/app/repositories/sqlite/notebook_store.py:302-323`: name the notebook-delete write operation.
- `backend/app/services/kg/scheduler.py:19-114`: add queued/waiting counts to existing pool statistics.
- `backend/tests/test_diag_unified.py:1-161`: six-command dispatch, compatibility, and offline-purity coverage.
- `backend/tests/test_background_jobs.py:15-110`: active job observation and cleanup on errors.
- `backend/tests/test_sqlite_database_component.py:19-53`: holder/waiter/re-entry/SQL privacy checks.
- `backend/tests/test_kg_scheduler.py:1-96`: waiting-count and reset/failure coverage.
- `README.md:763-794`, `README_zh.md:675-697`, `AGENTS.md:331`, `scripts/README.md:57-85`: synchronized operator and maintenance documentation.

---

### Task 1: Complete Historical Log Coverage

**Files:**
- Create: `scripts/diag_common.py`
- Create: `backend/tests/test_diag_common.py`
- Modify: `scripts/diag_slow.py:56-71,157-353,911-934`
- Modify: `scripts/diag_open_latency.py:34-50,200-225`
- Modify: `scripts/diag.py:23-107`
- Modify: `backend/tests/test_diag_unified.py:87-112`

**Interfaces:**
- Consumes: a log directory containing any combination of `<channel>.jsonl`, `<channel>-YYYY-MM-DD.jsonl`, `<channel>-YYYY-MM-DD.jsonl.gz`, and one-level per-user copies.
- Produces: `ChannelRecords(records, stats)`, `discover_channel_files(...)`, `read_channel(...)`, `iter_jsonl_file(...)`, and `normalize_http_path(...)` for all offline commands.

- [ ] **Step 1: Write failing layout, window, dedupe, malformed, and privacy tests**

Create `backend/tests/test_diag_common.py` with fixtures that deliberately place the same request into both plain and gzip files, plus unique legacy/daily/per-user records:

```python
from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"


def load_common():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("diag_common", SCRIPTS / "diag_common.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def line(identifier, ts, latency=10):
    return json.dumps({
        "id": identifier,
        "kind": "http",
        "channel": "requests",
        "method": "GET",
        "path": "/api/notebooks/nb-secret/sources",
        "latency_ms": latency,
        "ts": ts,
    }) + "\n"


def test_reads_legacy_daily_gzip_and_per_user_once(tmp_path):
    (tmp_path / "requests.jsonl").write_text(line("legacy", "2026-07-20T09:00:00"))
    duplicate = line("daily", "2026-07-21T09:00:00", 20)
    (tmp_path / "requests-2026-07-21.jsonl").write_text(duplicate + "{broken\n")
    with gzip.open(tmp_path / "requests-2026-07-21.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(duplicate)
    user = tmp_path / "user-abc"
    user.mkdir()
    (user / "requests-2026-07-21.jsonl").write_text(line("user", "2026-07-21T10:00:00", 30))

    common = load_common()
    result = common.read_channel(
        tmp_path,
        "requests",
        since_hours=48,
        now=datetime.fromisoformat("2026-07-21T12:00:00"),
    )

    assert [row["id"] for row in result.records] == ["legacy", "daily", "user"]
    assert result.stats.files == 4
    assert result.stats.malformed == 1
    assert result.stats.duplicates == 1
    assert result.stats.retained == 3


def test_window_and_limit_keep_only_matching_newest_records(tmp_path):
    rows = [line(str(index), f"2026-07-21T{index:02d}:00:00") for index in range(10)]
    (tmp_path / "events-2026-07-21.jsonl").write_text("".join(rows))
    common = load_common()
    result = common.read_channel(
        tmp_path,
        "events",
        since_hours=4,
        limit=2,
        now=datetime.fromisoformat("2026-07-21T10:00:00"),
    )
    assert [row["id"] for row in result.records] == ["8", "9"]
    assert result.stats.matched == 4
    assert result.stats.retained == 2


def test_http_path_normalization_does_not_return_identifiers():
    common = load_common()
    value = common.normalize_http_path(
        "/api/notebooks/nb-private123/sources/src-private456?token=secret"
    )
    assert value == "/api/notebooks/{id}/sources/{id}"
    assert "private" not in value
    assert "token" not in value
```

Extend `backend/tests/test_diag_unified.py` with a `latency` fixture containing only `events-YYYY-MM-DD.jsonl.gz`, and assert the score stage appears exactly once.

- [ ] **Step 2: Run the focused tests and verify the shared module is missing**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -p no:cacheprovider backend/tests/test_diag_common.py backend/tests/test_diag_unified.py -q
```

Expected: FAIL because `scripts/diag_common.py` does not exist and daily/gzip records are invisible to `latency`.

- [ ] **Step 3: Implement the bounded shared reader**

Create `scripts/diag_common.py` with these exact public types and functions:

```python
from __future__ import annotations

import gzip
import hashlib
import json
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

_DATED = re.compile(r"^(?P<channel>[a-z0-9_-]+)-(?P<day>\d{4}-\d{2}-\d{2})\.jsonl(?:\.gz)?$")
_ID_SEGMENT = re.compile(r"^(?:nb|src|ko|conv|user|mem|report|job)-[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class ScanStats:
    files: int
    parsed: int
    matched: int
    malformed: int
    duplicates: int
    retained: int
    truncated: bool


@dataclass(frozen=True)
class ChannelRecords:
    records: Tuple[Dict[str, Any], ...]
    stats: ScanStats


def discover_channel_files(log_dir: Path, channel: str,
                           explicit: Optional[Path] = None) -> Tuple[Path, ...]:
    roots = [Path(log_dir)]
    if Path(log_dir).is_dir():
        roots.extend(sorted(path for path in Path(log_dir).iterdir() if path.is_dir()))
    found = set()
    for root in roots:
        for name in (f"{channel}.jsonl",):
            path = root / name
            if path.is_file():
                found.add(path)
        found.update(path for path in root.glob(f"{channel}-*.jsonl") if path.is_file())
        found.update(path for path in root.glob(f"{channel}-*.jsonl.gz") if path.is_file())
    if explicit is not None and Path(explicit).is_file():
        found.add(Path(explicit))

    def order(path: Path) -> Tuple[str, float, str, str]:
        match = _DATED.match(path.name)
        day = match.group("day") if match else "0000-00-00"
        try:
            modified = path.stat().st_mtime
        except OSError:
            modified = 0.0
        return day, modified, str(path.parent), path.name

    return tuple(sorted(found, key=order))


def iter_jsonl_file(path: Path, *, tail_bytes: Optional[int] = None
                    ) -> Iterator[Tuple[Optional[Dict[str, Any]], bool, int]]:
    def lines() -> Iterator[str]:
        if str(path).endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
                yield from handle
            return
        with open(path, "rb") as handle:
            if tail_bytes is not None and path.stat().st_size > tail_bytes:
                handle.seek(-int(tail_bytes), 2)
                handle.readline()
            for raw_line in handle:
                yield raw_line.decode("utf-8", "replace")

    try:
        for raw in lines():
            raw_bytes = len(raw.encode("utf-8", "replace"))
            try:
                value = json.loads(raw)
            except (json.JSONDecodeError, ValueError, TypeError):
                yield None, True, raw_bytes
                continue
            yield value if isinstance(value, dict) else None, not isinstance(value, dict), raw_bytes
    except (OSError, EOFError, gzip.BadGzipFile):
        yield None, True, 0


def _record_key(channel: str, record: Dict[str, Any]) -> str:
    stable = [
        channel,
        str(record.get("id", "")),
        str(record.get("ts", "")),
        str(record.get("kind", "")),
        str(record.get("stage", "")),
        str(record.get("method", "")),
        str(record.get("path", "")),
        str(record.get("latency_ms", "")),
    ]
    return hashlib.sha256("\x1f".join(stable).encode("utf-8", "replace")).hexdigest()


def read_channel(log_dir: Path, channel: str, *, since_hours: Optional[float] = None,
                 limit: int = 50000, now: Optional[datetime] = None,
                 explicit: Optional[Path] = None,
                 max_input_bytes: int = 64 * 1024 * 1024,
                 deadline: Optional[float] = None) -> ChannelRecords:
    discovered = discover_channel_files(Path(log_dir), channel, explicit)
    selected = []
    selected_bytes = 0
    oversized = False
    for path in reversed(discovered):
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if selected and selected_bytes + size > max(1, int(max_input_bytes)):
            break
        selected.append(path)
        oversized = oversized or size > max(1, int(max_input_bytes))
        selected_bytes += min(size, max(1, int(max_input_bytes)))
        if selected_bytes >= max(1, int(max_input_bytes)):
            break
    paths = tuple(sorted(selected, key=lambda path: discovered.index(path)))
    retained = deque(maxlen=max(1, int(limit)))
    seen = set()
    parsed = matched = malformed = duplicates = 0
    truncated = len(paths) < len(discovered) or oversized
    decoded_bytes = 0
    stop = False
    cutoff = None if since_hours is None else (now or datetime.now()).timestamp() - since_hours * 3600
    for path in paths:
        for record, bad, raw_bytes in iter_jsonl_file(
            path, tail_bytes=None if str(path).endswith(".gz") else max_input_bytes
        ):
            decoded_bytes += raw_bytes
            if decoded_bytes > max(1, int(max_input_bytes)):
                truncated = True
                stop = True
                break
            if deadline is not None and time.monotonic() >= deadline:
                truncated = True
                stop = True
                break
            if bad or record is None:
                malformed += 1
                continue
            parsed += 1
            if cutoff is not None:
                try:
                    if datetime.fromisoformat(str(record.get("ts", ""))).timestamp() < cutoff:
                        continue
                except (TypeError, ValueError, OverflowError):
                    malformed += 1
                    continue
            matched += 1
            key = _record_key(channel, record)
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            if len(seen) > max(2 * int(limit), 1000):
                seen = {_record_key(channel, row) for row in retained}
            retained.append(record)
        if stop:
            break
    return ChannelRecords(
        tuple(retained),
        ScanStats(len(paths), parsed, matched, malformed, duplicates, len(retained), truncated),
    )


def normalize_http_path(path: str) -> str:
    clean = str(path).split("?", 1)[0]
    parts = []
    for segment in clean.split("/"):
        if _ID_SEGMENT.match(segment) or (len(segment) > 20 and any(ch.isdigit() for ch in segment)):
            parts.append("{id}")
        else:
            parts.append(segment[:80])
    return "/".join(parts)
```

Keep the reader output as metadata dictionaries only; callers decide which allow-listed fields to render.

- [ ] **Step 4: Refactor all historical readers onto the shared API**

In `diag_slow.py`, retain `_iter_jsonl(path)` as a compatibility wrapper that unpacks the three values from `iter_jsonl_file`, but replace the fixed `requests.jsonl`, `events.jsonl`, and `llm.jsonl` globs with `read_channel(Path(local_dir) / "logs", channel, since_hours=since.total_seconds() / 3600)`. Print `files`, `matched`, `malformed`, `duplicates`, `retained`, and `truncated` at each section header. Use `normalize_http_path()` instead of the local segment loop. The reader selects newest files first, tails an oversized plain current-day JSONL from a complete-line boundary, and bounds gzip decoding with the decoded-input counter/deadline. The 64 MiB limit and optional monotonic deadline are hard bounds, not report-only warnings.

In `diag_open_latency.py`, replace `_expand_request_files()` and its loop with:

```python
request_read = diag_common.read_channel(Path(local_dir) / "logs", "requests")
for rec in request_read.records:
    if rec.get("kind") != "http":
        continue
    path = str(rec.get("path", ""))
    if nb not in path:
        continue
    latency = rec.get("latency_ms")
    if isinstance(latency, (int, float)):
        key = f"{rec.get('method', '')} {diag_common.normalize_http_path(path)}"
        buckets.setdefault(key, []).append(float(latency))
```

In `diag.py`, make `_read_ask_stage()` call `read_channel()` using the parent and inferred `events` channel. Preserve `--log` as an explicit-path hint, and apply `--last` after filtering `kind == "ask_stage"`.

- [ ] **Step 5: Run focused and compatibility tests**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -p no:cacheprovider backend/tests/test_diag_common.py backend/tests/test_diag_unified.py backend/tests/test_event_logging.py backend/tests/test_debug_logs_days.py -q
```

Expected: PASS. A manual fixture containing only `requests-YYYY-MM-DD.jsonl` must no longer produce “0 requests” from `diag.py slow`.

- [ ] **Step 6: Commit the historical-reader fix**

```bash
git add scripts/diag_common.py scripts/diag_slow.py scripts/diag_open_latency.py scripts/diag.py backend/tests/test_diag_common.py backend/tests/test_diag_unified.py
git commit -m "fix: cover rotated logs in diagnostics"
```

---

### Task 2: Add the Process-Local Diagnostics Runtime

**Files:**
- Create: `backend/app/core/diagnostics_runtime.py`
- Create: `backend/tests/test_diagnostics_runtime.py`

**Interfaces:**
- Consumes: repository root plus callables returning readiness and concurrency dictionaries.
- Produces: `DiagnosticsRuntime`, `activate_runtime(...)`, `install_runtime(...)`, `current_runtime()`, `request_scope(...)`, `diagnostic_phase(...)`, `sql_scope(...)`, `job_scope(...)`, `begin_write_wait(...)`, `write_wait_cancelled(...)`, `write_acquired(...)`, and `write_released()`.

- [ ] **Step 1: Write failing registry, heartbeat, privacy, and signal tests**

Create tests that instantiate the runtime with a 20 ms snapshot interval and `enable_signal=False` for ordinary unit tests:

```python
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from app.core import diagnostics_runtime as diagnostics


def test_request_phase_sql_job_and_lock_snapshot_contains_metadata_only(tmp_path):
    runtime = diagnostics.DiagnosticsRuntime(
        tmp_path,
        readiness_provider=lambda: {"ready": True, "phase": "ready"},
        concurrency_provider=lambda: {"kg": {"active": 1, "maximum": 2, "waiting": 3}},
        interval_seconds=0.02,
        enable_signal=False,
    )
    secret = "SENSITIVE-SQL-VALUE"
    runtime.start()
    try:
        with diagnostics.install_runtime(runtime):
            with diagnostics.request_scope("req-test", "DELETE", "/api/notebooks/nb-private"):
                with diagnostics.diagnostic_phase("notebook_delete.db"):
                    waiter = diagnostics.begin_write_wait("notebook.delete")
                    diagnostics.write_acquired(waiter, "notebook.delete")
                    sql = f"DELETE FROM notebooks WHERE id='{secret}'"
                    with diagnostics.sql_scope("write", sql, "notebook.delete"):
                        snapshot = runtime.snapshot()
                    diagnostics.write_released()
                with diagnostics.job_scope("follow-up"):
                    job_snapshot = runtime.snapshot()
        encoded = json.dumps([snapshot, job_snapshot])
        assert secret not in encoded
        assert "nb-private" in encoded
        assert "DELETE FROM notebooks" not in encoded
        assert snapshot["write_lock"]["holder"]["operation"] == "notebook.delete"
        assert snapshot["active_sql"][0]["table"] == "notebooks"
        assert snapshot["active_requests"][0]["phase"] == "notebook_delete.db"
        assert job_snapshot["active_jobs"][0]["name"] == "follow-up"
    finally:
        runtime.stop()


def test_snapshot_is_atomic_and_heartbeat_advances(tmp_path):
    runtime = diagnostics.DiagnosticsRuntime(
        tmp_path,
        readiness_provider=lambda: {"ready": False, "phase": "warming"},
        concurrency_provider=lambda: {},
        interval_seconds=0.02,
        enable_signal=False,
    )
    runtime.start()
    try:
        deadline = time.time() + 2
        path = tmp_path / "runtime.json"
        first = None
        while time.time() < deadline:
            if path.exists():
                first = json.loads(path.read_text())
                break
            time.sleep(0.01)
        assert first is not None
        time.sleep(0.05)
        second = json.loads(path.read_text())
        assert second["heartbeat_at"] > first["heartbeat_at"]
        assert second["schema_version"] == 1
    finally:
        runtime.stop()


def test_sigusr1_appends_all_threads_without_terminating_child(tmp_path):
    code = """
import sys, threading, time
from pathlib import Path
from app.core.diagnostics_runtime import DiagnosticsRuntime
root = Path(sys.argv[1])
runtime = DiagnosticsRuntime(root, lambda: {}, lambda: {}, interval_seconds=1.0, enable_signal=True)
runtime.start()
threading.Thread(target=lambda: time.sleep(30), name='diag-child-worker', daemon=True).start()
print('READY', flush=True)
time.sleep(30)
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert child.stdout.readline().strip() == "READY"
        os.kill(child.pid, signal.SIGUSR1)
        dump = tmp_path / "thread-dumps.log"
        deadline = time.time() + 2
        while time.time() < deadline and (not dump.exists() or dump.stat().st_size == 0):
            time.sleep(0.02)
        assert child.poll() is None
        text = dump.read_text(errors="replace")
        assert text.count("Thread 0x") + text.count("Current thread 0x") >= 2
        assert "<lambda>" in text
    finally:
        child.terminate()
        child.wait(timeout=5)
```

- [ ] **Step 2: Run the runtime tests and verify the module is absent**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -p no:cacheprovider backend/tests/test_diagnostics_runtime.py -q
```

Expected: FAIL with an import error for `app.core.diagnostics_runtime`.

- [ ] **Step 3: Implement metadata normalization and context correlation**

Use schema version `1`, UTC ISO timestamps, `time.monotonic()` durations, and this exact SQL metadata contract:

```python
@dataclass(frozen=True)
class SqlMetadata:
    verb: str
    table: str
    fingerprint: str


def normalize_sql_metadata(sql: str) -> SqlMetadata:
    collapsed = " ".join(str(sql).strip().split())
    scrubbed = re.sub(r"'(?:''|[^'])*'|\b\d+(?:\.\d+)?\b", "?", collapsed)
    verb_match = re.match(r"(?i)^([A-Z]+)", scrubbed)
    table_match = re.search(
        r"(?i)\b(?:FROM|INTO|UPDATE|TABLE|JOIN)\s+[\"`\[]?([A-Za-z_][A-Za-z0-9_]*)",
        scrubbed,
    )
    return SqlMetadata(
        verb=verb_match.group(1).upper() if verb_match else "UNKNOWN",
        table=table_match.group(1) if table_match else "",
        fingerprint=hashlib.sha256(scrubbed.upper().encode("utf-8", "replace")).hexdigest()[:12],
    )
```

The diagnostic `ContextVar` value must contain only `request_id`, `job_id`, exact opaque `notebook_id`, and `phase`. `request_scope()` extracts a notebook identifier only from the allow-listed `/api/notebooks/{id}` route shape and stores the output route as `/api/notebooks/{id}`. It must never retain the query string.

- [ ] **Step 4: Implement bounded registries and no-op module wrappers**

Implement `DiagnosticsRuntime` with one internal `threading.Lock`, bounded dictionaries for active work, a `deque(maxlen=100)` for recent jobs, and these exact snapshot keys:

```python
{
    "schema_version": 1,
    "pid": os.getpid(),
    "process_started_at": "UTC ISO timestamp",
    "heartbeat_at": "UTC ISO timestamp",
    "last_state_change_at": "UTC ISO timestamp",
    "state_revision": 1,
    "snapshot_failures": 0,
    "readiness": {},
    "concurrency": {},
    "active_requests": [],
    "active_sql": [],
    "write_lock": {"holder": None, "waiters": []},
    "active_jobs": [],
    "recent_jobs": [],
}
```

Every active item contains `thread_id`, `started_at`, and computed `duration_ms`; request/job correlation fields are copied from the diagnostic context. The module-level wrappers obtain `current_runtime()` and become context-manager no-ops when no runtime is installed. `install_runtime(runtime)` installs the supplied object process-wide for its lexical test/application scope so worker threads see the same registry, and rejects a different already-installed runtime.

For re-entrant write ownership, `write_acquired()` increments `depth` when the current thread already owns the lock, while `write_released()` clears the holder only when depth reaches zero. `begin_write_wait()` does not add the owning thread as its own waiter. `write_wait_cancelled()` removes only the matching not-yet-acquired waiter and is also a no-op when diagnostics are inactive.

- [ ] **Step 5: Implement atomic snapshots and the signal lifecycle**

`start()` creates the diagnostics directory, truncates `thread-dumps.log` on clean startup, opens it in append mode, and starts one daemon named `diagnostics-snapshot`. When enabled on POSIX and called from `threading.main_thread()`, it registers `faulthandler.register(signal.SIGUSR1, file=handle, all_threads=True, chain=False)`; TestClient/non-main-thread activation records signal capture as unavailable rather than failing startup.

The writer calls both providers outside the registry lock, builds JSON with `allow_nan=False`, writes `runtime.json.tmp`, calls `flush()` and `os.fsync()`, then `os.replace(tmp, runtime.json)`. State changes set a wake event, but the writer enforces a minimum interval so bursts cannot write more than once per interval. Provider or filesystem exceptions increment `snapshot_failures` and never escape the thread. `stop()` sets an event, joins for at most two intervals, unregisters only the signal owned by this runtime, closes the dump handle, and performs no application shutdown.

`activate_runtime()` constructs one `DiagnosticsRuntime`, calls `start()`, installs it process-wide through `install_runtime()`, yields it, then always uninstalls and calls `stop()` in `finally`. This is the production lifecycle API; direct `start()` plus `install_runtime()` remains available for deterministic unit tests.

- [ ] **Step 6: Run the runtime tests**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -p no:cacheprovider backend/tests/test_diagnostics_runtime.py -q
```

Expected: PASS, including the child remaining alive after `SIGUSR1`.

- [ ] **Step 7: Commit the runtime core**

```bash
git add backend/app/core/diagnostics_runtime.py backend/tests/test_diagnostics_runtime.py
git commit -m "feat: add bounded diagnostics runtime"
```

---

### Task 3: Wire Requests, Jobs, Readiness, and Delete Phases

**Files:**
- Modify: `backend/app/main.py:38-51,100-109,148-188`
- Modify: `backend/app/services/background_jobs.py:37-65`
- Modify: `backend/app/services/notebook_catalog.py:380-391`
- Modify: `backend/tests/test_background_jobs.py:15-110`
- Modify: `backend/tests/test_diagnostics_runtime.py`

**Interfaces:**
- Consumes: Task 2 module-level scopes and `readiness.snapshot()`.
- Produces: a runtime automatically active during FastAPI lifespan, request entries visible before completion, background job entries, and explicit `notebook_delete.db` / `notebook_delete.files` phases.

- [ ] **Step 1: Write failing integration tests for a blocked request and job**

Add a TestClient route that blocks on an event. Start the request from a separate thread, poll `current_runtime().snapshot()`, and assert the request appears before the response completes with method, normalized path, request id, and phase. Add a background-job test using a blocked job and assert it is present under `active_jobs`, then moves to `recent_jobs` with `status == "done"`; repeat with an exception and expect `status == "error"`.

The blocked-request assertion must use the runtime data, not the completion-only JSONL request log:

```python
snapshot = diagnostics.current_runtime().snapshot()
active = snapshot["active_requests"]
assert len(active) == 1
assert active[0]["method"] == "GET"
assert active[0]["path"] == "/_diagnostics-test/block"
assert active[0]["phase"] == "http.dispatch"
```

- [ ] **Step 2: Run focused tests and verify no application hooks exist**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -p no:cacheprovider backend/tests/test_diagnostics_runtime.py backend/tests/test_background_jobs.py backend/tests/test_readiness_gate.py -q
```

Expected: FAIL because FastAPI and `background_jobs.submit()` do not enter diagnostics scopes.

- [ ] **Step 3: Activate diagnostics in the composed lifespan**

Add a provider in `main.py` that returns only numeric concurrency metadata:

```python
def _diagnostic_concurrency_snapshot() -> dict:
    from app.services.kg import scheduler
    from app.services.model_concurrency import current_model_concurrency

    result = {"kg": scheduler.stats()}
    state = current_model_concurrency()
    if state is not None:
        for name, snapshot in (
            ("llm", state.llm.snapshot()),
            ("embedding", state.embedding.snapshot()),
        ):
            result[name] = {
                "active": snapshot.active,
                "maximum": snapshot.maximum,
                "waiting": snapshot.waiting,
            }
    return result
```

Inside the composed lifespan, wrap `_lifespan(_app)` in:

```python
root_dir = Path(__file__).resolve().parents[2]
with diagnostics.activate_runtime(
    root_dir / ".local" / "diagnostics",
    readiness_provider=readiness.snapshot,
    concurrency_provider=_diagnostic_concurrency_snapshot,
):
    async with _lifespan(_app):
        yield
```

The activation must start before `_lifespan` creates the warm-up thread and stop after the inner lifespan exits.

- [ ] **Step 4: Wrap the existing request middleware without changing completion logs**

Keep the existing `request_id`, response header, error logging, and latency logic. Enclose dispatch and completion handling in:

```python
with diagnostics.request_scope(request_id, request.method, request.url.path):
    with diagnostics.diagnostic_phase("http.dispatch"):
        response = await call_next(request)
```

The request scope's `finally` must run for success, application exception, and disconnect. Do not add request body/header/client data to the runtime snapshot.

- [ ] **Step 5: Observe background jobs and notebook-delete phases**

In `background_jobs._run()`, wrap the existing try/except/finally body with `diagnostics.job_scope(label)`. Do not change `submit()`'s signature, context copy, daemon setting, notification behavior, or exception isolation.

In `NotebookCatalogService.delete_notebook()` use:

```python
with diagnostics.diagnostic_phase("notebook_delete.db"):
    file_paths = self._store.delete_row_and_orphan_embeddings(notebook_id)
with diagnostics.diagnostic_phase("notebook_delete.files"):
    for file_path in file_paths:
        _delete_source_file(file_path)
    _delete_notebook_asset_dir(self._storage_dir(), notebook_id)
```

- [ ] **Step 6: Run request/job/readiness regression tests**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -p no:cacheprovider backend/tests/test_diagnostics_runtime.py backend/tests/test_background_jobs.py backend/tests/test_readiness_gate.py backend/tests/test_request_user_ctx.py -q
```

Expected: PASS. Existing `X-Request-Id`, readiness gating, user context propagation, and pending notifications remain unchanged.

- [ ] **Step 7: Commit application lifecycle hooks**

```bash
git add backend/app/main.py backend/app/services/background_jobs.py backend/app/services/notebook_catalog.py backend/tests/test_diagnostics_runtime.py backend/tests/test_background_jobs.py
git commit -m "feat: expose active requests and jobs to DFX"
```

---

### Task 4: Instrument SQLite Work and Re-entrant Write Contention

**Files:**
- Modify: `backend/app/repositories/sqlite/database.py:12-128`
- Modify: `backend/app/repositories/sqlite/notebook_store.py:302-323`
- Modify: `backend/tests/test_sqlite_database_component.py:19-53`
- Modify: `backend/tests/test_sqlite_connection_reuse.py:1-180`

**Interfaces:**
- Consumes: Task 2 SQL and write-lock registry calls.
- Produces: cursor-level active SQL metadata and exact outer-holder/waiter visibility while preserving `SqliteDatabase.write()` semantics and the public `write_lock` object.

- [ ] **Step 1: Write failing holder, waiter, re-entry, and SQL-privacy tests**

Use two threads and events. The holder enters `db.write(operation="holder-op")`; the waiter attempts `db.write(operation="waiter-op")`. While blocked, assert one holder and one waiter with positive duration. Add a nested same-thread `write()` assertion that holder depth becomes `2` without adding a waiter, then returns to `1` and finally `None`.

For SQL privacy, register a SQLite function that blocks during `SELECT diag_block(?) FROM notebooks`, pass a sentinel parameter, and assert while blocked:

```python
active = runtime.snapshot()["active_sql"]
assert active[0]["verb"] == "SELECT"
assert active[0]["table"] == "notebooks"
assert len(active[0]["fingerprint"]) == 12
encoded = json.dumps(active)
assert "SELECT diag_block" not in encoded
assert "PRIVATE-SQL-PARAMETER" not in encoded
```

- [ ] **Step 2: Run focused tests and verify contention is invisible**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -p no:cacheprovider backend/tests/test_sqlite_database_component.py backend/tests/test_sqlite_connection_reuse.py -q
```

Expected: FAIL because `write()` accepts no operation and runtime lock/SQL entries remain empty.

- [ ] **Step 3: Add a diagnostic cursor without exposing parameters**

Add `_DiagnosticCursor(sqlite3.Cursor)` that remembers only `SqlMetadata`, mode, and logical operation. Wrap `execute`, `executemany`, `fetchone`, `fetchmany`, `fetchall`, and iterator `__next__` calls in `diagnostics.sql_scope(mode, sql_text, operation)`; the scope receives SQL text but never the parameters. Clear the remembered SQL in `close()`.

Override `_Conn.cursor()`, `execute()`, and `executemany()` so implicit connection cursors are `_DiagnosticCursor`. Wrap `executescript()` once at connection level. `_new_connection(mode="read")` assigns `_diag_mode` and `_diag_operation`; `connect()` uses `read`, and `write()` uses `write`.

The key call form is:

```python
def execute(self, sql, parameters=()):
    cursor = self.cursor()
    return cursor.execute(sql, parameters)


def fetchall(self):
    with diagnostics.sql_scope(self._diag_mode, self._diag_sql, self._diag_operation):
        return super().fetchall()
```

All wrappers catch diagnostics errors inside `diagnostics_runtime`; SQLite exceptions and return values pass through unchanged.

- [ ] **Step 4: Replace `with RLock` by semantically equivalent observed acquire/release**

Change `write()` to `write(self, *, operation: str = "sqlite.write")`. Use the exact same `self.write_lock` object and acquire it before creating a connection:

```python
waiter = diagnostics.begin_write_wait(operation)
acquired = False
try:
    self.write_lock.acquire()
    acquired = True
    diagnostics.write_acquired(waiter, operation)
    conn = self._new_connection(mode="write", operation=operation)
    try:
        with conn:
            yield conn
    finally:
        conn.close()
finally:
    if acquired:
        diagnostics.write_released()
        self.write_lock.release()
```

If `acquire()` raises, remove the waiter through a new `write_wait_cancelled(waiter)` wrapper. The diagnostics runtime owns re-entry depth; do not replace `RLock` with a custom lock class.

- [ ] **Step 5: Name the notebook-delete write operation**

Change only the notebook delete transaction call to:

```python
with self.database.write(operation="notebook.delete") as db:
```

Leave every SQL statement and DB-first/files-second ordering unchanged.

- [ ] **Step 6: Run SQLite semantics and architecture guards**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -p no:cacheprovider backend/tests/test_sqlite_database_component.py backend/tests/test_sqlite_connection_reuse.py backend/tests/test_db_concurrency.py backend/tests/test_sqlite_write_optimization.py backend/tests/test_repository_runtime_identity.py -q
```

Expected: PASS. The tests must prove rollback/commit behavior, thread-local read reuse, independent write connections, exact public lock identity, and re-entry are preserved.

- [ ] **Step 7: Commit SQLite instrumentation**

```bash
git add backend/app/repositories/sqlite/database.py backend/app/repositories/sqlite/notebook_store.py backend/tests/test_sqlite_database_component.py backend/tests/test_sqlite_connection_reuse.py
git commit -m "feat: expose SQLite contention to DFX"
```

---

### Task 5: Complete Concurrency-Gate Evidence

**Files:**
- Modify: `backend/app/services/kg/scheduler.py:19-114`
- Modify: `backend/tests/test_kg_scheduler.py:1-96`
- Modify: `backend/tests/test_diagnostics_runtime.py`

**Interfaces:**
- Consumes: existing `scheduler.stats()` and Task 3 concurrency provider.
- Produces: `window_waiting` and `job_waiting` in KG stats, aligned with existing model/embedding `active`, `maximum`, and `waiting` snapshots.

- [ ] **Step 1: Write failing queued-work tests**

Configure each pool with one worker, block the first task, submit a second task, and assert:

```python
stats = scheduler.stats()
assert stats == {
    "window_active": 1,
    "window_max": 1,
    "window_waiting": 1,
    "job_active": 0,
    "job_max": 1,
    "job_waiting": 0,
}
```

Repeat for the job pool. Add cancellation and submit-failure tests so waiting returns to zero exactly once.

- [ ] **Step 2: Run scheduler tests and verify waiting keys are absent**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -p no:cacheprovider backend/tests/test_kg_scheduler.py backend/tests/test_model_concurrency.py -q
```

Expected: FAIL on missing KG waiting counters; existing LLM and embedding counters remain green.

- [ ] **Step 3: Add cancellation-safe queued counters**

For each pool, increment waiting before `executor.submit()`. The worker moves one count from waiting to active under `_active_lock`; its `finally` decrements active. Attach a done callback that decrements waiting only when the future was cancelled before the worker marked its per-submission ticket as started. On submit failure, decrement waiting synchronously.

Return all six fields from `stats()`:

```python
return {
    "window_active": _window_active,
    "window_max": _window_max,
    "window_waiting": _window_waiting,
    "job_active": _job_active,
    "job_max": _job_max,
    "job_waiting": _job_waiting,
}
```

Keep `contextvars.copy_context()` placement and the two-pool deadlock-avoidance architecture unchanged.

- [ ] **Step 4: Verify the runtime snapshot includes all gates**

Extend the runtime integration test to activate model concurrency, queue KG work, force one waiting task in each applicable gate, and assert `runtime.json` contains numeric-only `kg`, `llm`, and `embedding` dictionaries.

- [ ] **Step 5: Run concurrency tests**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -p no:cacheprovider backend/tests/test_kg_scheduler.py backend/tests/test_model_concurrency.py backend/tests/test_embed_concurrency.py backend/tests/test_diagnostics_runtime.py -q
```

Expected: PASS with no leaked active/waiting counters after success, cancellation, or exception.

- [ ] **Step 6: Commit concurrency evidence**

```bash
git add backend/app/services/kg/scheduler.py backend/tests/test_kg_scheduler.py backend/tests/test_diagnostics_runtime.py
git commit -m "feat: report diagnostic concurrency queues"
```

---

### Task 6: Build the Read-only SQLite Analyzer

**Files:**
- Create: `scripts/diag_db.py`
- Create: `backend/tests/test_diag_db.py`

**Interfaces:**
- Consumes: SQLite path and optional exact notebook id retained locally.
- Produces: `collect_db_evidence(db_path, notebook_id=None, deadline_seconds=4.0) -> dict`, `render_db_report(evidence) -> str`, and `main(argv=None) -> int`.

- [ ] **Step 1: Write failing analyzer tests against a deliberately unindexed cascade**

Create a temporary WAL database with `notebooks`, indexed `sources(notebook_id)`, unindexed `legacy_children(notebook_id)`, `knowledge_embeddings`, and small FTS-like tables. Assert:

```python
evidence = diag_db.collect_db_evidence(db_path, notebook_id="nb-private")
missing = {(row["table"], tuple(row["columns"])) for row in evidence["missing_fk_indexes"]}
assert ("legacy_children", ("notebook_id",)) in missing
assert ("sources", ("notebook_id",)) not in missing
assert evidence["files"]["database_bytes"] > 0
assert evidence["journal_mode"].lower() == "wal"
assert any("SCAN" in row["detail"].upper() for row in evidence["delete_plan"])
assert evidence["mutations_executed"] == 0
assert connection.execute("SELECT COUNT(*) FROM notebooks").fetchone()[0] == 1
```

Add a separate `journal_mode=DELETE` fixture whose second connection holds `BEGIN EXCLUSIVE`, then assert the analyzer returns within 1.5 seconds with a degraded-evidence entry instead of raising. Do not use the WAL fixture for this assertion because WAL readers are allowed to proceed beside a writer.

- [ ] **Step 2: Run the tests and verify the analyzer is missing**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -p no:cacheprovider backend/tests/test_diag_db.py -q
```

Expected: FAIL because `scripts/diag_db.py` does not exist.

- [ ] **Step 3: Implement safe connection and deadline primitives**

Open with a URI containing `mode=ro`, `timeout=1.0`, then execute only:

```python
connection.execute("PRAGMA query_only = ON")
connection.execute("PRAGMA foreign_keys = ON")
connection.execute("PRAGMA busy_timeout = 1000")
```

Install a progress handler that returns nonzero after the passed monotonic deadline. Convert `sqlite3.OperationalError` containing `interrupted`, `locked`, or `busy` into a structured `degraded` entry containing only probe name and exception class/category; do not print arbitrary exception text.

- [ ] **Step 4: Implement FK-index and delete-plan evidence**

Enumerate tables from `sqlite_master`, use `PRAGMA foreign_key_list`, `PRAGMA index_list`, and `PRAGMA index_info`, and consider a foreign key covered only when its ordered child columns are a leftmost prefix of an enabled index. Quote schema-derived identifiers by doubling `"`; never interpolate a user-supplied identifier.

For delete coverage, compile without executing:

```sql
EXPLAIN QUERY PLAN DELETE FROM notebooks WHERE id = ?
```

Also compile the three explicit product statements:

```sql
EXPLAIN QUERY PLAN SELECT file_path FROM sources WHERE notebook_id = ?
EXPLAIN QUERY PLAN DELETE FROM knowledge_embeddings WHERE notebook_id = ?
EXPLAIN QUERY PLAN DELETE FROM kg_objects_fts WHERE notebook_id = ?
```

Return plan rows as `{id, parent, detail}` after replacing the exact notebook id with `{id}`. List tables referencing `notebooks` and flag `SCAN` details tied to unindexed child keys.

- [ ] **Step 5: Implement file/table scale evidence and standalone rendering**

Read database, `-wal`, and `-shm` sizes with `Path.stat()`. Query `page_count`, `freelist_count`, `page_size`, and `journal_mode`. Attempt the read-only eponymous `dbstat` aggregation:

```sql
SELECT name, SUM(pgsize) AS bytes, COUNT(*) AS pages
FROM dbstat
GROUP BY name
ORDER BY bytes DESC
LIMIT 20
```

If unavailable or interrupted, report degradation and continue. Never call `wal_checkpoint`, even in passive mode. `render_db_report()` prints pseudonymized notebook identity, largest tables, missing FK indexes, relevant scans, WAL size, and targeted recommendations.

- [ ] **Step 6: Run analyzer tests and a real read-only smoke**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -p no:cacheprovider backend/tests/test_diag_db.py -q
python3 scripts/diag_db.py --db .local/silicon_notebook.db
```

Expected: tests PASS. The smoke exits zero whether the local DB exists or reports a clear missing-file degradation; it performs no mutation.

- [ ] **Step 7: Commit the DB analyzer**

```bash
git add scripts/diag_db.py backend/tests/test_diag_db.py
git commit -m "feat: add read-only SQLite DFX analyzer"
```

---

### Task 7: Collect Linux Process and Non-terminating Stack Evidence

**Files:**
- Create: `scripts/diag_process.py`
- Create: `backend/tests/test_diag_process.py`

**Interfaces:**
- Consumes: repository root, optional PID, diagnostics directory, health URLs, and a replaceable proc adapter.
- Produces: `ProcAdapter`, `resolve_backend_pid(...)`, `sample_process(...)`, `load_runtime_snapshot(...)`, `capture_thread_dump(...)`, `parse_thread_dump(...)`, and `probe_http(...)`.

- [ ] **Step 1: Write failing fake-`/proc`, stale-snapshot, HTTP, and signal tests**

Use a `FakeProcAdapter` with zero, one, and two Uvicorn candidates. A valid candidate command includes `python -m uvicorn app.main:app --workers 1`; its cwd is either `<repo>/backend` or `<repo>`. Assert automatic resolution only for exactly one matching PID, and that ambiguous resolution returns sanitized candidate PID/cwd-relative metadata without selecting one.

Add fixtures for process state, RSS, thread count, FD count, I/O bytes, per-task `D` state, and two CPU samples. Add snapshot cases for valid, malformed, wrong PID, wrong schema, and heartbeat older than 6 seconds.

For signal capture, reuse a child runtime, record the initial file offset, call `capture_thread_dump()`, assert only appended bytes are returned, assert the child remains alive, and verify the parsed dump includes at least two thread blocks.

- [ ] **Step 2: Run focused tests and verify the collector is missing**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -p no:cacheprovider backend/tests/test_diag_process.py -q
```

Expected: FAIL because `scripts/diag_process.py` does not exist.

- [ ] **Step 3: Implement replaceable `/proc` collection**

`ProcAdapter` must read only `/proc/<pid>/{cmdline,cwd,stat,status,io,fd,task}` and skip permission/race failures. Parse `/proc/<pid>/stat` by locating the final `)` before splitting fields, because process names may contain spaces or parentheses. Return:

```python
{
    "pid": pid,
    "state": "S",
    "cpu_percent": 0.0,
    "rss_bytes": 0,
    "threads": 0,
    "fds": 0,
    "read_bytes": 0,
    "write_bytes": 0,
    "d_state_threads": 0,
    "uptime_seconds": 0.0,
}
```

Compute CPU from two process-tick/monotonic samples separated by 200 ms, using `os.sysconf("SC_CLK_TCK")`. Tests inject the adapter and sampler clock, so macOS development does not require real `/proc`.

- [ ] **Step 4: Implement safe PID resolution and liveness probes**

Match both `uvicorn` and `app.main:app`, require cwd equal to repository root or its `backend` child, and reject the diagnostics command's own PID. Explicit `--pid` still verifies process existence but does not require command matching; the report labels it operator-supplied.

Use `urllib.request.urlopen` with a 750 ms timeout for `/api/ready` and frontend `/`. Return only URL role, HTTP status, elapsed milliseconds, and one of `ok`, `timeout`, `refused`, or `error`; never retain response bodies.

- [ ] **Step 5: Implement runtime validation and serialized stack capture**

Validate schema `1`, PID equality, JSON object shape, and heartbeat age. Mark snapshots older than 6 seconds stale and do not use their active-work fields for high-confidence conclusions.

On Linux, serialize collectors with `fcntl.flock()` on `.local/diagnostics/incident.lock`. Record `thread-dumps.log` size, send `os.kill(pid, signal.SIGUSR1)`, poll for growth for at most 1 second, and return only the appended segment capped at 512 KiB. After successful capture, if the file exceeds 8 MiB, truncate it in place under the same advisory lock. If the signal or file step fails, return structured degradation and continue.

- [ ] **Step 6: Parse and sanitize faulthandler stacks**

Parse `Thread 0x...` and `Current thread 0x...` blocks into thread ids and frame lines. Retain repository-relative frames plus allow-listed standard/client-library frames containing `sqlite3`, `threading`, `asyncio`, `anyio`, `urllib`, `httpx`, `openai`, `shutil`, or `os.py`. Replace absolute repository prefixes with `<repo>/`; collapse all other frames to a count. Do not parse locals because faulthandler does not emit them.

- [ ] **Step 7: Run process/signal tests**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -p no:cacheprovider backend/tests/test_diag_process.py backend/tests/test_diagnostics_runtime.py -q
```

Expected: PASS on macOS through fake `/proc`, with the real child-signal test skipped only when `SIGUSR1` is unavailable.

- [ ] **Step 8: Commit process collection**

```bash
git add scripts/diag_process.py backend/tests/test_diag_process.py
git commit -m "feat: collect live process evidence for DFX"
```

---

### Task 8: Rank Causes and Render a Bounded Incident Report

**Files:**
- Create: `scripts/diag_rules.py`
- Create: `scripts/diag_incident.py`
- Create: `backend/tests/test_diag_incident.py`

**Interfaces:**
- Consumes: runtime/process/stack/log/DB evidence from Tasks 1, 6, and 7.
- Produces: `Finding`, `diagnose(evidence)`, `render_incident(evidence, findings, limit_bytes=32768)`, `collect_incident(...)`, and `main(argv=None)`.

- [ ] **Step 1: Write failing deterministic-rule and sanitization tests**

Create evidence fixtures for SQLite holder/waiter/scan, external-model wait, pool saturation, event-loop stall, filesystem delete, WAL pressure, high-CPU repeated frame, low-CPU `D` state, and frontend/backend split health. For the delete fixture include:

```python
DELETE_INCIDENT = {
    "runtime": {
        "active_requests": [{
            "request_id": "req-private",
            "method": "DELETE",
            "path": "/api/notebooks/{id}",
            "notebook_id": "nb-private",
            "phase": "notebook_delete.db",
            "thread_id": 101,
            "duration_ms": 12000,
        }],
        "write_lock": {
            "holder": {"thread_id": 101, "operation": "notebook.delete", "duration_ms": 11000},
            "waiters": [{"thread_id": 202, "operation": "sqlite.write", "duration_ms": 9000}],
        },
        "active_sql": [{"thread_id": 101, "verb": "DELETE", "table": "notebooks", "fingerprint": "abc123def456", "duration_ms": 10000}],
        "concurrency": {},
    },
    "stacks": {"65": ["<repo>/backend/app/repositories/sqlite/database.py:120 in write"]},
    "db": {
        "missing_fk_indexes": [{"table": "legacy_children", "columns": ["notebook_id"]}],
        "delete_plan": [{"detail": "SCAN legacy_children"}],
        "files": {"database_bytes": 1000, "wal_bytes": 200},
        "degraded": [],
    },
    "process": {"cpu_percent": 2.0, "d_state_threads": 0},
    "health": {"backend": "timeout", "frontend": "ok"},
    "logs": {"malformed": 0, "slow_requests": []},
    "degraded": [],
}
```

Assert the first finding is SQLite write blocking, includes holder/waiter/scan evidence and a missing-index recommendation, and does not contain `nb-private` or `req-private`. Add `SENSITIVE-PROMPT`, `Bearer secret`, and an absolute home path to unused evidence fields and assert none occur in the report. Assert UTF-8 encoded report length is at most 32768 bytes.

Also add `test_live_delete_lock_holder_waiter_report`. It creates a temporary WAL database through `SqliteDatabase`, with one `notebooks` row and an unindexed `legacy_children(notebook_id REFERENCES notebooks(id) ON DELETE CASCADE)` row. Under `install_runtime(runtime)`, a holder thread enters `db.write(operation="notebook.delete")`, registers a blocking zero-argument SQLite function, and executes `DELETE FROM notebooks WHERE diag_hold() = 1`; the function waits on an event and returns `0`, so no row is deleted. A waiter thread concurrently enters `db.write(operation="sqlite.write")`.

Poll until the runtime snapshot contains both holder and waiter, then build stack lines from `sys._current_frames()[holder.ident]` with `traceback.extract_stack`, call `collect_db_evidence()` while the WAL reader can proceed, and pass the resulting real snapshot/stack/DB evidence to `diagnose()` and `render_incident()`. In `finally`, release the function, join both threads, stop the runtime, and assert the notebook still exists. The test must assert:

```python
assert findings[0].code == "sqlite_write_blocking"
assert "legacy_children" in report
assert "SCAN" in report
assert "nb-live-private" not in report
assert len(report.encode("utf-8")) <= 32768
assert holder.is_alive() is False
assert waiter.is_alive() is False
```

Use `format(thread_id, "x")` to correlate runtime decimal thread ids with the lowercase hexadecimal ids returned by `parse_thread_dump()`; this same mapping is used by production rules.

- [ ] **Step 2: Run the tests and verify rule/report modules are missing**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -p no:cacheprovider backend/tests/test_diag_incident.py -q
```

Expected: FAIL on missing `diag_rules` / `diag_incident`.

- [ ] **Step 3: Implement exact finding scores and confidence labels**

Use `Finding(code, title, score, confidence, evidence, next_action)`. Build scores only from allow-listed evidence:

- SQLite blocking: holder duration ≥5 s = 45; any waiter = +20; holder SQLite frame = +20; delete-plan scan/missing FK index = +15.
- Slow SQL/cascade scan: active SQL ≥5 s = 40; `SCAN` = +30; missing FK index = +20; implicated table ≥100 MiB = +10.
- External model/network: allowed client-network frame = 50; LLM active = +20; model error/slow log near capture = +15.
- Pool saturation: active equals maximum = 45; waiting >0 = +35; waiting ≥maximum = +10.
- Event-loop stall: backend timeout with live PID = 30; main thread repeats one blocking repository frame = +50; CPU below 20% = +10.
- Filesystem cleanup: delete phase is `notebook_delete.files` = 45; `unlink`/`rmtree` frame = +40; write lock already free = +10.
- WAL pressure: WAL ≥256 MiB = 35; long active read = +30; WAL growth across samples ≥64 MiB = +20.
- CPU loop: CPU ≥80% of one core = 45; repeated same application frame = +40.
- I/O wait: CPU <20%, I/O counters grow, and `d_state_threads > 0` = 85.
- Backend/front-end split: one probe `ok` and the other non-`ok` = 60.

Confidence is `high` at score ≥80, `medium` at ≥55, otherwise `low`. Sort by descending score then stable code, return at most three findings by default, and never label a low-confidence single signal as root cause.

- [ ] **Step 4: Implement pseudonyms, section budgets, and report rendering**

Use a report-local keyed hash to map opaque ids to stable labels such as `notebook#1`, `request#1`, and `job#1`. Render only fields selected by each section; never recursively dump evidence dictionaries.

Allocate the 32768-byte default budget in priority order: 8 KiB top findings, 6 KiB active work/lock, 8 KiB relevant stacks, 5 KiB DB/host, 3 KiB logs, and 2 KiB missing/degraded evidence. Truncate at complete UTF-8 line boundaries and always retain the title, capture timestamp, top findings, and degraded-evidence section.

- [ ] **Step 5: Implement bounded incident orchestration**

`collect_incident()` uses one monotonic 10-second deadline. It resolves paths/PID, validates runtime, captures stacks, samples `/proc`, probes both services, reads a bounded recent request/event/LLM window by passing that same deadline into `read_channel()`, and invokes `collect_db_evidence()` with the exact notebook id only when a live delete request or `--notebook` supplies it.

Use these CLI defaults:

```python
parser.add_argument("--root", default=".")
parser.add_argument("--local", default="")
parser.add_argument("--pid", type=int)
parser.add_argument("--notebook", default="")
parser.add_argument("--since", type=float, default=2.0)
parser.add_argument("--output-limit-kib", type=int, default=32)
parser.add_argument("--verbose", action="store_true")
```

Parse only safe `.env` keys `PORT`, `FRONTEND_PORT`, and `BACKEND_HOST`; do not retain or print any other values. A missing/ambiguous PID skips signaling but still collects offline logs and DB evidence. Every exception becomes a category-only degraded entry.

- [ ] **Step 6: Run rule, output, and synthetic-delete tests**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -p no:cacheprovider backend/tests/test_diag_incident.py backend/tests/test_diag_db.py backend/tests/test_diag_process.py -q
```

Expected: PASS. The synthetic report names the holder, waiter, relevant stack, scan, missing index, and next action while containing none of the original opaque ids or sentinels.

- [ ] **Step 7: Commit incident analysis**

```bash
git add scripts/diag_rules.py scripts/diag_incident.py backend/tests/test_diag_incident.py
git commit -m "feat: rank and render live DFX incidents"
```

---

### Task 9: Expose the Complete Six-command Suite

**Files:**
- Modify: `scripts/diag.py:1-185`
- Modify: `backend/tests/test_diag_unified.py:1-161`

**Interfaces:**
- Consumes: `diag_incident.main`, `diag_db.main`, and existing `diag_open_latency.main` plus current handlers.
- Produces: `incident`, `slow`, `latency`, `open`, `db`, and `base-recall` under one dispatcher while retaining direct script invocation.

- [ ] **Step 1: Update failing command-surface and purity tests**

Change the dispatch contract to:

```python
assert set(diag.SUBCOMMANDS) == {
    "incident", "slow", "latency", "open", "db", "base-recall"
}
```

Parametrize `--help` for all six commands and assert zero exit. Extend offline-purity checks by building arguments from pytest's `tmp_path`: use PID `2147483647` for the nonexistent-process case, the temporary directory for `open --local`, and `tmp_path / "missing.db"` for `db --db`. After each command, assert `app.core.config` and `app.services.sqlite_repository` are absent from `sys.modules`. Keep a separate assertion that `base-recall` remains the only lazy app-importing handler.

- [ ] **Step 2: Run dispatcher tests and verify commands are missing**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -p no:cacheprovider backend/tests/test_diag_unified.py -q
```

Expected: FAIL because `incident`, `open`, and `db` are not registered.

- [ ] **Step 3: Add lazy stdlib handlers and help text**

Each new handler mirrors `_cmd_slow`: add `scripts/` to `sys.path`, import its sibling only inside the handler, temporarily replace `sys.argv`, call `main()`, and restore `sys.argv` in `finally`.

Register in this order:

```python
SUBCOMMANDS = {
    "incident": _cmd_incident,
    "slow": _cmd_slow,
    "latency": _cmd_latency,
    "open": _cmd_open,
    "db": _cmd_db,
    "base-recall": _cmd_base_recall,
}
```

Document `incident` as the primary live-capture command. Preserve bare/leading-flag dispatch to `slow`, unknown-command exit code `2`, and direct standalone script behavior.

- [ ] **Step 4: Run all diagnostic command tests and smoke help**

Run:

```bash
PYTHONPATH=backend python3 -m pytest -p no:cacheprovider backend/tests/test_diag_common.py backend/tests/test_diag_db.py backend/tests/test_diag_process.py backend/tests/test_diag_incident.py backend/tests/test_diag_unified.py -q
python3 scripts/diag.py --help
python3 scripts/diag.py incident --help
python3 scripts/diag.py db --help
python3 scripts/diag.py open --help
```

Expected: PASS/exit zero. Help lists exactly six subcommands and does not import application modules.

- [ ] **Step 5: Commit the unified surface**

```bash
git add scripts/diag.py backend/tests/test_diag_unified.py
git commit -m "feat: unify production DFX commands"
```

---

### Task 10: Synchronize Documentation and Run Full Verification

**Files:**
- Modify: `README.md:763-794`
- Modify: `README_zh.md:675-697`
- Modify: `AGENTS.md:331`
- Modify: `scripts/README.md:57-85`

**Interfaces:**
- Consumes: the verified CLI and runtime behavior from Tasks 1-9.
- Produces: equivalent English/Chinese operator guidance plus the repository maintenance contract.

- [ ] **Step 1: Update all four documentation surfaces together**

Document this primary production flow in both READMEs and `scripts/README.md`:

```bash
ssh <production-host>
cd <silicon-notebook-repository>
python3 scripts/diag.py incident
```

Include the retry `python3 scripts/diag.py incident --pid <backend-pid>`, the six-command matrix, daily/gzip/per-user coverage, `SIGUSR1` non-terminating semantics, `.local/diagnostics/runtime.json`, output-size/pseudonymization rules, no-root/no-third-party/no-restart/no-DB-write guarantees, and the instruction to review even sanitized text before sharing outside the trusted team.

Update `AGENTS.md` to make these invariants binding for future changes: runtime metadata never includes content/SQL parameters, diagnostics wrappers are exception-safe, the reporter stays app-import-free, dump retention remains bounded, old standalone scripts keep working, and README pairs stay synchronized.

- [ ] **Step 2: Run documentation and whitespace checks**

Run:

```bash
git diff --check
rg -n "diag.py incident|SIGUSR1|32 KiB|daily|gzip|per-user" README.md README_zh.md AGENTS.md scripts/README.md
```

Expected: no whitespace errors; every required concept appears in the English README, Chinese README, agent contract, and script guide where applicable.

- [ ] **Step 3: Run the complete backend and repository gate**

Run:

```bash
bash scripts/check.sh
```

Expected: exit `0`; contracts, backend tests, and frontend checks all pass without network/model credentials.

- [ ] **Step 4: Run the explicit frontend production build**

Run:

```bash
cd frontend && npm run build
```

Expected: exit `0` and a successful Next.js production build.

- [ ] **Step 5: Run the synthetic-delete and production-shaped acceptance smokes**

First run the deterministic disposable-database scenario implemented in `test_live_delete_lock_holder_waiter_report`, which uses a real `DiagnosticsRuntime`, `SqliteDatabase`, holder thread, waiter thread, unindexed cascade schema, and report renderer:

```bash
PYTHONPATH=backend python3 -m pytest -p no:cacheprovider backend/tests/test_diag_incident.py::test_live_delete_lock_holder_waiter_report -q
```

Expected: PASS within 10 seconds; its report is at most 32768 UTF-8 bytes and identifies holder, waiter, stack, and scan/missing-index evidence without exact identifiers or sentinels.

Then, while the ordinary local `npm run start` deployment is running, execute:

```bash
python3 scripts/diag.py incident
```

Expected: command completes within 10 seconds, captures a non-terminating stack dump, and leaves the backend alive. An idle deployment may correctly report no high-confidence blockage; it must not invent a root cause.

- [ ] **Step 6: Review the final diff for accidental product changes**

Run:

```bash
git status --short
git diff --stat
git diff -- README.md README_zh.md AGENTS.md scripts/README.md
```

Expected: only planned diagnostics, tests, and synchronized docs changed; no frontend UI, API schema, migration, `.env`, production database, generated artifact, or `fangan_done.md` change is present.

- [ ] **Step 7: Commit documentation and final verification state**

```bash
git add README.md README_zh.md AGENTS.md scripts/README.md
git commit -m "docs: document production DFX workflow"
```

---

## Self-review Coverage Map

| Design requirement | Implementing task |
| --- | --- |
| Daily, gzip, legacy, per-user logs; malformed/dedupe/window bounds | Task 1 |
| Active requests, phases, jobs, readiness, heartbeat | Tasks 2-3 |
| Atomic `runtime.json`, stale detection, bounded dump file | Tasks 2 and 7 |
| Non-terminating all-thread `SIGUSR1` stack | Tasks 2 and 7 |
| SQLite SQL metadata with no params; holder/waiters; RLock re-entry | Task 4 |
| KG/LLM/embedding active/max/waiting | Tasks 3 and 5 |
| Read-only DB/WAL/table/FK-index/delete-plan evidence | Task 6 |
| PID discovery, `/proc`, health probes, CPU/RSS/FD/I/O/`D` state | Task 7 |
| Ranked Top 3, uncertainty, privacy, pseudonyms, 32 KiB budget | Task 8 |
| Notebook-delete holder/waiter/stack/scan recommendation | Tasks 4, 6, and 8 |
| Missing PID, failed signal, stale snapshot, DB busy, corrupt logs | Tasks 1, 6, 7, and 8 |
| Six coherent commands and old-script compatibility | Task 9 |
| No app import except `base-recall` | Tasks 1, 6-9 |
| No root, third-party dependency, restart, DB mutation, or auto-remediation | Tasks 2, 6-8 |
| README/README_zh/AGENTS/scripts docs sync and full gates | Task 10 |
