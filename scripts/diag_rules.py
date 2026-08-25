#!/usr/bin/env python3
"""Deterministic metadata-only diagnosis and bounded incident rendering."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence


MIB = 1024 * 1024
DEFAULT_LIMIT_BYTES = 32 * 1024
SECTION_BUDGETS = {
    "findings": 8 * 1024,
    "active": 6 * 1024,
    "stacks": 8 * 1024,
    "db_host": 5 * 1024,
    "logs": 3 * 1024,
    "degraded": 2 * 1024,
}
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.#-]{1,128}$")
_SAFE_FRAME = re.compile(
    r"^(?:<repo>/[A-Za-z0-9_./-]+|<library:[A-Za-z0-9_.-]+>):"
    r"\d+ in [A-Za-z0-9_<>. -]{1,128}$"
)
_SAFE_ROUTE_SEGMENTS = frozenset({
    "", "api", "analytics", "ask", "cancel", "cells", "conversations",
    "deep-report", "diagnostics", "download", "events", "export", "graph",
    "health", "indexing-pipeline", "jobs", "knowledge", "memory", "notebooks", "preview",
    "ready", "reports", "search", "shared", "sources", "status", "stream",
    "tables", "unified-kg",
})
_NETWORK_FRAME_TOKENS = (
    "<library:httpx>",
    "<library:urllib>",
    "<library:openai>",
    "/core/llm.py:",
    "/model_concurrency.py:",
)
_SQLITE_FRAME_TOKENS = (
    "<library:sqlite3>",
    "/repositories/sqlite/",
    "/sqlite/database.py:",
)
_FILESYSTEM_FRAME_TOKENS = (
    "<library:shutil>",
    " in unlink",
    " in rmtree",
)
_REPOSITORY_BLOCKING_TOKENS = (
    "/repositories/",
    "/sqlite/",
    "<library:sqlite3>",
)
_DB_TERMINAL_DEGRADATIONS = frozenset(
    {"source_changed", "locked", "busy", "deadline", "interrupted"}
)


@dataclass(frozen=True)
class Finding:
    code: str
    title: str
    score: int
    confidence: str
    evidence: tuple[str, ...]
    next_action: str


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(row for row in value if isinstance(row, Mapping))


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if result == result and abs(result) != float("inf") else default


def _safe_name(value: Any, fallback: str = "unknown") -> str:
    if value is None or isinstance(value, bool):
        return fallback
    text = str(value)
    return text if _SAFE_NAME.fullmatch(text) else fallback


def _safe_frame(value: Any) -> str | None:
    text = str(value)
    return text if _SAFE_FRAME.fullmatch(text) else None


def _safe_route(value: Any) -> str:
    text = str(value)
    if not text.startswith("/api/") or "?" in text or "#" in text:
        return "/api/{redacted}"
    parts = text.split("/")
    if all(part in _SAFE_ROUTE_SEGMENTS or part in {"{id}", "{token}"} for part in parts):
        return text
    return "/api/{redacted}"


def _frames(evidence: Mapping[str, Any]) -> tuple[str, ...]:
    output: list[str] = []
    for values in _mapping(evidence.get("stacks")).values():
        if not isinstance(values, (list, tuple)):
            continue
        for value in values:
            frame = _safe_frame(value)
            if frame is not None:
                output.append(frame)
    return tuple(output)


def _thread_frames(evidence: Mapping[str, Any], thread_id: Any) -> tuple[str, ...]:
    if isinstance(thread_id, bool):
        return ()
    try:
        key = format(int(thread_id), "x")
    except (TypeError, ValueError, OverflowError):
        return ()
    values = _mapping(evidence.get("stacks")).get(key, ())
    if not isinstance(values, (list, tuple)):
        return ()
    return tuple(frame for value in values if (frame := _safe_frame(value)) is not None)


def _repeated_frames(evidence: Mapping[str, Any], thread_id: Any = None) -> tuple[str, ...]:
    samples = evidence.get("stack_samples")
    if not isinstance(samples, (list, tuple)) or len(samples) < 2:
        return ()
    key = None
    if thread_id is not None and not isinstance(thread_id, bool):
        try:
            key = format(int(thread_id), "x")
        except (TypeError, ValueError, OverflowError):
            return ()
    sample_maps = [_mapping(sample) for sample in samples]
    thread_keys = (
        {key}
        if key is not None
        else set.intersection(*(set(sample) for sample in sample_maps))
    )
    repeated: set[str] = set()
    for thread_key in thread_keys:
        common: set[str] | None = None
        for sample in sample_maps:
            values = sample.get(thread_key, ())
            current = {
                frame
                for value in values if isinstance(values, (list, tuple))
                if (frame := _safe_frame(value)) is not None
            }
            common = current if common is None else common & current
        repeated.update(common or ())
    return tuple(sorted(repeated))


def _health_result(value: Any) -> str:
    if isinstance(value, str):
        return value if value in {"ok", "timeout", "refused", "error"} else "unknown"
    result = _mapping(value).get("result")
    return result if result in {"ok", "timeout", "refused", "error"} else "unknown"


def _db_usable(evidence: Mapping[str, Any]) -> bool:
    db = _mapping(evidence.get("db"))
    if db.get("evidence_complete") is False:
        return False
    return not any(
        str(row.get("category", "")) in _DB_TERMINAL_DEGRADATIONS
        for row in _rows(db.get("degraded"))
    )


def _runtime_usable(evidence: Mapping[str, Any]) -> bool:
    return evidence.get("runtime_fresh") is not False and bool(
        _mapping(evidence.get("runtime"))
    )


def _confidence(score: int) -> str:
    if score >= 80:
        return "high"
    if score >= 55:
        return "medium"
    return "low"


def _finding(
    code: str,
    title: str,
    score: int,
    evidence: Sequence[str],
    next_action: str,
) -> Finding | None:
    if score <= 0:
        return None
    return Finding(
        code=code,
        title=title,
        score=score,
        confidence=_confidence(score),
        evidence=tuple(evidence),
        next_action=next_action,
    )


def _scan_rows(db: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    output: list[tuple[str, str]] = []
    relevant = _rows(db.get("relevant_scans"))
    source = relevant or _rows(db.get("delete_plan"))
    for row in source:
        detail = str(row.get("detail", ""))
        if "SCAN" not in detail.upper():
            continue
        table = _safe_name(row.get("table"))
        if table == "unknown":
            tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]{0,127}", detail)
            table = _safe_name(tokens[-1] if tokens else "unknown")
        output.append((table, f"SCAN {table}"))
    return tuple(sorted(set(output)))


def _missing_indexes(db: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(sorted({
        _safe_name(row.get("table"))
        for row in _rows(db.get("missing_fk_indexes"))
        if _safe_name(row.get("table")) != "unknown"
    }))


def _sqlite_blocking(evidence: Mapping[str, Any]) -> Finding | None:
    if not _runtime_usable(evidence):
        return None
    runtime = _mapping(evidence.get("runtime"))
    lock = _mapping(runtime.get("write_lock"))
    holder = _mapping(lock.get("holder"))
    waiters = _rows(lock.get("waiters"))
    score = 0
    chain: list[str] = []
    if _number(holder.get("duration_ms")) >= 5_000:
        score += 45
        chain.append(f"write holder active {_number(holder.get('duration_ms')) / 1000:.1f}s")
    if waiters:
        score += 20
        chain.append(f"{len(waiters)} writer waiter(s)")
    holder_frames = _thread_frames(evidence, holder.get("thread_id"))
    sqlite_frame = next(
        (frame for frame in holder_frames if any(token in frame for token in _SQLITE_FRAME_TOKENS)),
        None,
    )
    if sqlite_frame:
        score += 20
        chain.append(f"holder stack: {sqlite_frame}")
    db = _mapping(evidence.get("db"))
    scans = _scan_rows(db) if _db_usable(evidence) else ()
    missing = _missing_indexes(db) if _db_usable(evidence) else ()
    if scans or missing:
        score += 15
        if scans:
            chain.append(scans[0][1])
        if missing:
            chain.append(f"missing FK index: {', '.join(missing[:4])}")
    target = missing[0] if missing else (scans[0][0] if scans else "the implicated child table")
    return _finding(
        "sqlite_write_blocking",
        "SQLite write transaction blocking",
        score,
        chain,
        f"Inspect the delete plan and add a verified missing index for {target}; do not restart or mutate data from diagnostics.",
    )


def _slow_sql(evidence: Mapping[str, Any]) -> Finding | None:
    if not _runtime_usable(evidence) or not _db_usable(evidence):
        return None
    runtime = _mapping(evidence.get("runtime"))
    active = tuple(
        row for row in _rows(runtime.get("active_sql"))
        if _number(row.get("duration_ms")) >= 5_000
    )
    if not active:
        return None
    db = _mapping(evidence.get("db"))
    active_tables = {_safe_name(row.get("table")) for row in active}
    delete_active = any(
        str(row.get("verb", "")).upper() == "DELETE"
        and _safe_name(row.get("table")) == "notebooks"
        for row in active
    )
    scans = tuple(
        row for row in _scan_rows(db)
        if delete_active or row[0] in active_tables
    )
    missing = tuple(
        table for table in _missing_indexes(db)
        if delete_active or table in active_tables
    )
    score = 40 if active else 0
    chain = [f"{len(active)} SQL operation(s) active at least 5s"] if active else []
    if scans:
        score += 30
        chain.append(scans[0][1])
    if missing:
        score += 20
        chain.append(f"missing FK index: {', '.join(missing[:4])}")
    implicated = {
        _safe_name(row.get("table")) for row in active
    } | {table for table, _detail in scans} | set(missing)
    large = next(
        (
            (_safe_name(row.get("name")), int(_number(row.get("bytes"))))
            for row in _rows(db.get("largest_tables"))
            if _safe_name(row.get("name")) in implicated
            and _number(row.get("bytes")) >= 100 * MIB
        ),
        None,
    )
    if large:
        score += 10
        chain.append(f"{large[0]} is at least 100 MiB")
    target = missing[0] if missing else (scans[0][0] if scans else "the active table")
    return _finding(
        "slow_sql_cascade",
        "Slow SQL or notebook-delete cascade scan",
        score,
        chain,
        f"Review EXPLAIN output and index coverage for {target} before changing the schema.",
    )


def _external_wait(evidence: Mapping[str, Any]) -> Finding | None:
    frame = next(
        (frame for frame in _frames(evidence) if any(token in frame for token in _NETWORK_FRAME_TOKENS)),
        None,
    )
    score = 50 if frame else 0
    chain = [f"client/network stack: {frame}"] if frame else []
    concurrency = (
        _mapping(_mapping(evidence.get("runtime")).get("concurrency"))
        if _runtime_usable(evidence)
        else {}
    )
    llm = _mapping(concurrency.get("llm"))
    if _number(llm.get("active")) > 0:
        score += 20
        chain.append(f"LLM active={int(_number(llm.get('active')))}")
    if _mapping(evidence.get("logs")).get("model_recent_error_or_slow") is True:
        score += 15
        chain.append("recent model error/slow metadata present")
    return _finding(
        "external_model_wait",
        "External model or network wait",
        score,
        chain,
        "Check the configured model endpoint from the host and inspect sanitized LLM timing/error metadata.",
    )


def _pool_saturation(evidence: Mapping[str, Any]) -> Finding | None:
    if not _runtime_usable(evidence):
        return None
    concurrency = _mapping(_mapping(evidence.get("runtime")).get("concurrency"))
    best: tuple[int, str, list[str]] | None = None
    for group_name, raw_group in sorted(concurrency.items()):
        group = _mapping(raw_group)
        candidates = [("", "active", "maximum", "waiting")]
        if group_name == "kg":
            candidates = [
                ("window", "window_active", "window_max", "window_waiting"),
                ("job", "job_active", "job_max", "job_waiting"),
            ]
        for suffix, active_key, maximum_key, waiting_key in candidates:
            active = int(_number(group.get(active_key)))
            maximum = int(_number(group.get(maximum_key)))
            waiting = int(_number(group.get(waiting_key)))
            score = 0
            chain: list[str] = []
            label = _safe_name(f"{group_name}.{suffix}".rstrip("."))
            if maximum > 0 and active == maximum:
                score += 45
                chain.append(f"{label} active={active}, maximum={maximum}")
            if waiting > 0:
                score += 35
                chain.append(f"{label} waiting={waiting}")
            if maximum > 0 and waiting >= maximum:
                score += 10
                chain.append("waiting count reached the configured maximum")
            candidate = (score, label, chain)
            if best is None or (candidate[0], candidate[1]) > (best[0], best[1]):
                best = candidate
    if best is None:
        return None
    return _finding(
        "pool_saturation",
        "Concurrency gate saturation",
        best[0],
        best[2],
        f"Inspect queued work and configured capacity for {best[1]}; avoid increasing limits before checking downstream pressure.",
    )


def _event_loop_stall(evidence: Mapping[str, Any]) -> Finding | None:
    process = _mapping(evidence.get("process"))
    health = _mapping(evidence.get("health"))
    score = 0
    chain: list[str] = []
    if _number(process.get("pid")) > 0 and _health_result(health.get("backend")) == "timeout":
        score += 30
        chain.append("backend timed out while the process remained live")
    repeated = next(
        (
            frame for frame in _repeated_frames(evidence, evidence.get("main_thread_id"))
            if any(token in frame for token in _REPOSITORY_BLOCKING_TOKENS)
        ),
        None,
    )
    if repeated:
        score += 50
        chain.append(f"main thread repeated: {repeated}")
    if "cpu_percent" in process and _number(process.get("cpu_percent"), 100) < 20:
        score += 10
        chain.append(f"CPU={_number(process.get('cpu_percent')):.1f}%")
    return _finding(
        "event_loop_stall",
        "Possible event-loop stall",
        score,
        chain,
        "Capture another non-terminating thread dump and move confirmed synchronous repository work off the event loop.",
    )


def _filesystem_cleanup(evidence: Mapping[str, Any]) -> Finding | None:
    runtime = _mapping(evidence.get("runtime")) if _runtime_usable(evidence) else {}
    phases = {str(row.get("phase", "")) for row in _rows(runtime.get("active_requests"))}
    score = 0
    chain: list[str] = []
    if "notebook_delete.files" in phases:
        score += 45
        chain.append("delete request is in notebook_delete.files")
    frame = next(
        (frame for frame in _frames(evidence) if any(token in frame for token in _FILESYSTEM_FRAME_TOKENS)),
        None,
    )
    if frame:
        score += 40
        chain.append(f"filesystem stack: {frame}")
    lock = _mapping(runtime.get("write_lock"))
    if lock and lock.get("holder") is None:
        score += 10
        chain.append("SQLite write lock is free")
    return _finding(
        "filesystem_cleanup",
        "Filesystem cleanup bottleneck",
        score,
        chain,
        "Inspect storage mount latency and source-file counts; do not delete files from the diagnostic command.",
    )


def _wal_pressure(evidence: Mapping[str, Any]) -> Finding | None:
    if not _db_usable(evidence):
        return None
    db = _mapping(evidence.get("db"))
    files = _mapping(db.get("files"))
    score = 0
    chain: list[str] = []
    wal_bytes = int(_number(files.get("wal_bytes")))
    if wal_bytes >= 256 * MIB:
        score += 35
        chain.append(f"WAL={wal_bytes // MIB} MiB")
    reads = tuple(
        row for row in _rows(_mapping(evidence.get("runtime")).get("active_sql"))
        if str(row.get("mode", "")).lower() == "read"
        and _number(row.get("duration_ms")) >= 5_000
    ) if _runtime_usable(evidence) else ()
    if reads:
        score += 30
        chain.append(f"{len(reads)} long active read(s)")
    growth = int(_number(db.get("wal_growth_bytes")))
    if growth >= 64 * MIB:
        score += 20
        chain.append(f"WAL grew {growth // MIB} MiB across samples")
    return _finding(
        "wal_pressure",
        "WAL or checkpoint pressure",
        score,
        chain,
        "Identify long readers and observe WAL growth; diagnostics must not run a checkpoint.",
    )


def _cpu_loop(evidence: Mapping[str, Any]) -> Finding | None:
    process = _mapping(evidence.get("process"))
    score = 0
    chain: list[str] = []
    cpu = _number(process.get("cpu_percent"))
    if cpu >= 80:
        score += 45
        chain.append(f"CPU={cpu:.1f}% of one core or more")
    repeated = next(
        (frame for frame in _repeated_frames(evidence) if frame.startswith("<repo>/")),
        None,
    )
    if repeated:
        score += 40
        chain.append(f"application frame repeated: {repeated}")
    return _finding(
        "cpu_loop",
        "CPU-bound loop or computation",
        score,
        chain,
        "Compare another process/thread sample and profile the repeated application frame outside the incident collector.",
    )


def _io_wait(evidence: Mapping[str, Any]) -> Finding | None:
    process = _mapping(evidence.get("process"))
    cpu = _number(process.get("cpu_percent"), 100)
    io_growth = (
        _number(process.get("read_bytes_delta"))
        + _number(process.get("write_bytes_delta"))
        + _number(process.get("io_growth_bytes"))
    )
    d_state = int(_number(process.get("d_state_threads")))
    if cpu >= 20 or io_growth <= 0 or d_state <= 0:
        return None
    return _finding(
        "io_wait",
        "Disk or network-filesystem wait",
        85,
        (f"CPU={cpu:.1f}%", f"I/O grew by {int(io_growth)} bytes", f"D-state threads={d_state}"),
        "Check host and storage latency for the affected process; do not signal or restart it from diagnostics.",
    )


def _service_split(evidence: Mapping[str, Any]) -> Finding | None:
    health = _mapping(evidence.get("health"))
    backend = _health_result(health.get("backend"))
    frontend = _health_result(health.get("frontend"))
    if "unknown" in {backend, frontend} or (backend == "ok") == (frontend == "ok"):
        return None
    affected = "frontend" if backend == "ok" else "backend"
    return _finding(
        "service_split",
        f"{affected.capitalize()}-specific service outage",
        60,
        (f"backend={backend}, frontend={frontend}",),
        f"Check the {affected} service logs and listener without restarting either service from diagnostics.",
    )


_RULES = (
    _sqlite_blocking,
    _slow_sql,
    _external_wait,
    _pool_saturation,
    _event_loop_stall,
    _filesystem_cleanup,
    _wal_pressure,
    _cpu_loop,
    _io_wait,
    _service_split,
)


def diagnose(evidence: Mapping[str, Any], limit: int = 3) -> list[Finding]:
    """Rank at most ``limit`` deterministic hypotheses from allow-listed metadata."""

    safe_evidence = evidence if isinstance(evidence, Mapping) else {}
    findings = [finding for rule in _RULES if (finding := rule(safe_evidence)) is not None]
    findings.sort(key=lambda finding: (-finding.score, finding.code))
    return findings[: max(0, min(int(limit), 20))]


class _Pseudonyms:
    def __init__(self, captured_at: str) -> None:
        self._key = hashlib.sha256(
            ("silicon-notebook:incident:" + captured_at).encode("utf-8", "replace")
        ).digest()
        self._labels: dict[tuple[str, bytes], str] = {}
        self._counts: dict[str, int] = {}

    def label(self, kind: str, value: Any) -> str | None:
        if value in (None, ""):
            return None
        safe_kind = kind if kind in {"notebook", "request", "job", "source", "user"} else "id"
        digest = hashlib.blake2b(
            str(value).encode("utf-8", "replace"), key=self._key, digest_size=16
        ).digest()
        key = (safe_kind, digest)
        if key not in self._labels:
            self._counts[safe_kind] = self._counts.get(safe_kind, 0) + 1
            self._labels[key] = f"{safe_kind}#{self._counts[safe_kind]}"
        return self._labels[key]


def _duration(value: Any) -> str:
    return f"{max(0.0, _number(value)) / 1000:.1f}s"


def _section(title: str, lines: Iterable[str], budget: int) -> list[str]:
    output = [title]
    used = len((title + "\n").encode("utf-8"))
    for line in lines:
        safe = str(line).replace("\r", " ").replace("\n", " ")
        encoded = (safe + "\n").encode("utf-8", "replace")
        if used + len(encoded) > max(0, budget):
            marker = "  ... section truncated"
            if used + len((marker + "\n").encode("utf-8")) <= max(0, budget):
                output.append(marker)
            break
        output.append(safe)
        used += len(encoded)
    if len(output) == 1:
        output.append("  none")
    return output


def _append_with_limit(output: list[str], lines: Sequence[str], limit: int, reserve: int = 0) -> None:
    for line in lines:
        candidate = "\n".join(output + [line, ""]).encode("utf-8")
        if len(candidate) + reserve > limit:
            return
        output.append(line)


def _degraded_lines(evidence: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    resolution = _mapping(evidence.get("pid_resolution"))
    if resolution.get("status") in {"missing", "ambiguous", "scan_incomplete"}:
        lines.append("  - process evidence unavailable; retry with an explicitly verified --pid")
    process = _mapping(evidence.get("process"))
    if process.get("status") in {"unavailable", "deadline", "identity_changed", "scan_incomplete"}:
        lines.append("  - process evidence unavailable")
    if evidence.get("runtime_fresh") is False:
        lines.append("  - runtime snapshot stale or unavailable; live state was excluded from diagnosis")
    for source, value in (
        ("incident", evidence.get("degraded")),
        ("database", _mapping(evidence.get("db")).get("degraded")),
    ):
        for row in _rows(value):
            category = _safe_name(row.get("category"), "unavailable")
            probe = _safe_name(row.get("source", row.get("probe", "collector")), "collector")
            lines.append(f"  - {source}.{probe}: {category}")
    logs = _mapping(evidence.get("logs"))
    malformed = int(_number(logs.get("malformed")))
    if malformed:
        lines.append(f"  - logs: {malformed} malformed record(s) skipped")
    return sorted(set(lines)) or ["  - none reported"]


def render_incident(
    evidence: Mapping[str, Any],
    findings: Sequence[Finding],
    limit_bytes: int = DEFAULT_LIMIT_BYTES,
) -> str:
    """Render one deterministic UTF-8 block without recursively dumping evidence."""

    safe = evidence if isinstance(evidence, Mapping) else {}
    canonical = {finding.code: finding for finding in diagnose(safe, limit=20)}
    trusted_findings = [
        canonical[finding.code]
        for finding in findings
        if isinstance(finding, Finding)
        and finding.code in canonical
        and finding == canonical[finding.code]
    ][:3]
    limit = max(512, min(int(limit_bytes), DEFAULT_LIMIT_BYTES))
    captured = str(safe.get("captured_at", "unavailable"))
    if not re.fullmatch(r"[0-9T:+.Z-]{10,40}", captured):
        captured = "unavailable"
    pseudonyms = _Pseudonyms(captured)
    process = _mapping(safe.get("process"))
    pid = int(_number(process.get("pid")))
    header = [
        "silicon-notebook DFX incident report",
        f"Captured: {captured}   PID: {pid if pid > 0 else 'unavailable'}",
        "",
    ]

    finding_lines: list[str] = []
    action_lines: list[str] = []
    for index, finding in enumerate(trusted_findings, 1):
        finding_lines.append(
            f"  {index}. [{finding.confidence}] {finding.title} (score {finding.score})"
        )
        for item in finding.evidence:
            finding_lines.append(f"     Evidence: {item}")
        action_lines.append(f"  {index}. {finding.next_action}")
    if not finding_lines:
        finding_lines.append("  No multi-signal diagnosis reached a useful confidence level.")
        action_lines.append("  1. Re-run incident capture while the operation is visibly stuck.")

    runtime = _mapping(safe.get("runtime")) if _runtime_usable(safe) else {}
    active_lines: list[str] = []
    for row in _rows(runtime.get("active_requests")):
        request = pseudonyms.label("request", row.get("request_id"))
        notebook = pseudonyms.label("notebook", row.get("notebook_id"))
        fields = [
            str(row.get("method")) if row.get("method") in {"GET", "POST", "PATCH", "DELETE", "PUT"} else "request",
            _safe_route(row.get("path")),
            f"phase={_safe_name(row.get('phase'))}",
            f"duration={_duration(row.get('duration_ms'))}",
        ]
        if request:
            fields.append(request)
        if notebook:
            fields.append(notebook)
        active_lines.append("  - " + " ".join(fields))
    lock = _mapping(runtime.get("write_lock"))
    holder = _mapping(lock.get("holder"))
    if holder:
        active_lines.append(
            f"  - write holder thread=0x{format(int(_number(holder.get('thread_id'))), 'x')} "
            f"operation={_safe_name(holder.get('operation'))} duration={_duration(holder.get('duration_ms'))}"
        )
    waiters = _rows(lock.get("waiters"))
    if waiters:
        active_lines.append(f"  - write waiters={len(waiters)}")
        for row in waiters[:16]:
            active_lines.append(
                f"    thread=0x{format(int(_number(row.get('thread_id'))), 'x')} "
                f"operation={_safe_name(row.get('operation'))} duration={_duration(row.get('duration_ms'))}"
            )
    for row in _rows(runtime.get("active_jobs"))[:32]:
        job = pseudonyms.label("job", row.get("job_id"))
        notebook = pseudonyms.label("notebook", row.get("notebook_id"))
        active_lines.append(
            "  - job " + " ".join(
                value for value in (
                    job,
                    notebook,
                    f"name={_safe_name(row.get('name', row.get('job_name')))}",
                    f"duration={_duration(row.get('duration_ms'))}",
                ) if value
            )
        )

    stack_lines: list[str] = []
    for thread_id, values in sorted(_mapping(safe.get("stacks")).items()):
        if not re.fullmatch(r"[0-9a-f]{1,32}", str(thread_id)) or not isinstance(values, (list, tuple)):
            continue
        retained = [frame for value in values if (frame := _safe_frame(value)) is not None]
        if retained:
            stack_lines.append(f"  - thread 0x{thread_id}")
            stack_lines.extend(f"    {frame}" for frame in retained)

    db = _mapping(safe.get("db"))
    files = _mapping(db.get("files"))
    db_lines = [
        f"  - database={int(_number(files.get('database_bytes')))} bytes; WAL={int(_number(files.get('wal_bytes')))} bytes"
    ]
    for table, detail in _scan_rows(db):
        db_lines.append(f"  - plan: {detail}")
    for table in _missing_indexes(db):
        columns = next(
            (
                [_safe_name(column) for column in row.get("columns", ()) if _safe_name(column) != "unknown"]
                for row in _rows(db.get("missing_fk_indexes"))
                if _safe_name(row.get("table")) == table
            ),
            [],
        )
        db_lines.append(f"  - missing FK index: {table}({', '.join(columns)})")
    if "cpu_percent" in process:
        db_lines.append(
            f"  - host: CPU={_number(process.get('cpu_percent')):.1f}% "
            f"D-state threads={int(_number(process.get('d_state_threads')))}"
        )
    health = _mapping(safe.get("health"))
    db_lines.append(
        f"  - probes: backend={_health_result(health.get('backend'))} "
        f"frontend={_health_result(health.get('frontend'))}"
    )

    logs = _mapping(safe.get("logs"))
    log_lines = [
        f"  - retained metadata records={int(_number(logs.get('retained')))}",
        f"  - malformed records={int(_number(logs.get('malformed')))}",
    ]
    if logs.get("model_recent_error_or_slow") is True:
        log_lines.append("  - recent model error/slow metadata present")

    sections = [
        _section("Confidence-ranked diagnoses", finding_lines, SECTION_BUDGETS["findings"]),
        _section("Observations", active_lines, SECTION_BUDGETS["active"]),
        _section("Relevant stacks", stack_lines, SECTION_BUDGETS["stacks"]),
        _section("Database and host signals", db_lines, SECTION_BUDGETS["db_host"]),
        _section("Log metadata", log_lines, SECTION_BUDGETS["logs"]),
        _section("Missing/degraded evidence", _degraded_lines(safe), SECTION_BUDGETS["degraded"]),
        _section("Safe next commands/actions", action_lines, SECTION_BUDGETS["findings"]),
    ]
    finding_section = sections[0]
    optional_sections = sections[1:-2]
    missing = sections[-2]
    actions = sections[-1]
    finding_minimum = finding_section[:2]
    missing_minimum = missing[:2]
    actions_minimum = actions[:1]

    def encoded_size(lines: Sequence[str]) -> int:
        return len(("\n".join(lines) + "\n").encode("utf-8"))

    output = list(header)
    mandatory_tail = missing_minimum + [""] + actions_minimum
    _append_with_limit(output, finding_minimum, limit, reserve=encoded_size(mandatory_tail))
    _append_with_limit(
        output,
        finding_section[len(finding_minimum):] + [""],
        limit,
        reserve=encoded_size(mandatory_tail),
    )
    for section in optional_sections:
        _append_with_limit(
            output,
            section + [""],
            limit,
            reserve=encoded_size(mandatory_tail),
        )
    _append_with_limit(
        output,
        missing_minimum,
        limit,
        reserve=encoded_size(actions_minimum),
    )
    _append_with_limit(
        output,
        missing[len(missing_minimum):] + [""],
        limit,
        reserve=encoded_size(actions_minimum),
    )
    _append_with_limit(output, actions_minimum, limit)
    _append_with_limit(output, actions[len(actions_minimum):], limit)
    report = "\n".join(output).rstrip("\n") + "\n"
    while len(report.encode("utf-8")) > limit and len(output) > 1:
        output.pop(-2 if output[-1] == "" else -1)
        report = "\n".join(output).rstrip("\n") + "\n"
    return report


__all__ = ["Finding", "diagnose", "render_incident"]
