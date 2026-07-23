#!/usr/bin/env python3
"""Collect and render one bounded, read-only production incident report."""

from __future__ import annotations

import argparse
import errno
import ipaddress
import os
import re
import stat as stat_module
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from diag_common import normalize_http_path, read_channel
from diag_db import collect_db_evidence
from diag_process import (
    capture_thread_dump,
    load_runtime_snapshot,
    parse_thread_dump,
    probe_http,
    resolve_backend_pid,
    sample_process,
)
from diag_rules import diagnose, render_incident


MAX_INCIDENT_SECONDS = 10.0
MAX_DB_SECONDS = 1.0
MAX_OUTPUT_BYTES = 32 * 1024
SAFE_ENV_KEYS = frozenset({"PORT", "FRONTEND_PORT", "BACKEND_HOST"})
_PORT = re.compile(r"^[0-9]{1,5}$")
_HOST = re.compile(r"^[A-Za-z0-9.:[\]-]{1,128}$")
_MAX_ENV_BYTES = 64 * 1024
_ENV_READ_CHUNK = 8 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def read_safe_env(
    path: Path | str,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    on_degraded: Callable[[str], None] | None = None,
) -> dict[str, str]:
    """Read only non-secret listener settings from a small dotenv file."""

    result: dict[str, str] = {}
    descriptor: int | None = None

    def degrade(category: str) -> dict[str, str]:
        if on_degraded is not None:
            on_degraded(category)
        return {}

    if deadline is not None and clock() >= deadline:
        return degrade("deadline")
    try:
        descriptor = os.open(
            os.fspath(path),
            os.O_RDONLY | _NOFOLLOW | _NONBLOCK | _CLOEXEC,
        )
        metadata = os.fstat(descriptor)
        if not stat_module.S_ISREG(metadata.st_mode):
            return degrade("unsafe_type")
        if metadata.st_uid != os.getuid() or metadata.st_nlink != 1:
            return degrade("unsafe_link")
        if metadata.st_size > _MAX_ENV_BYTES:
            return degrade("oversize")
        chunks: list[bytes] = []
        consumed = 0
        while consumed <= _MAX_ENV_BYTES:
            if deadline is not None and clock() >= deadline:
                return degrade("deadline")
            chunk = os.read(
                descriptor,
                min(_ENV_READ_CHUNK, _MAX_ENV_BYTES + 1 - consumed),
            )
            if not chunk:
                break
            chunks.append(chunk)
            consumed += len(chunk)
        raw = b"".join(chunks)
        if len(raw) > _MAX_ENV_BYTES:
            return degrade("oversize")
        try:
            text = raw.decode("utf-8", "strict")
        except UnicodeDecodeError:
            return degrade("malformed")
        for index, raw_line in enumerate(text.splitlines()):
            if index >= 512:
                return degrade("oversize")
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, raw_value = line.partition("=")
            key = key.strip()
            if separator != "=" or key not in SAFE_ENV_KEYS:
                continue
            value = raw_value.strip().strip("\"'")
            if key in {"PORT", "FRONTEND_PORT"}:
                if _PORT.fullmatch(value) and 0 < int(value) <= 65535:
                    result[key] = value
            elif _HOST.fullmatch(value):
                result[key] = value
        return result
    except FileNotFoundError:
        return {}
    except PermissionError:
        return degrade("permission")
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return degrade("unsafe_link")
        return degrade("unavailable")
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _load_safe_env(
    root: Path,
    deadline: float,
    evidence: dict[str, Any],
    clock: Callable[[], float],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in (root / ".env", root / "backend" / ".env"):
        result.update(
            read_safe_env(
                path,
                deadline=deadline,
                clock=clock,
                on_degraded=lambda category: _degraded(evidence, "env", category),
            )
        )
    return result


def _loopback_host(value: str) -> str:
    if value.lower() == "localhost":
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        return "127.0.0.1"
    if not address.is_loopback:
        return "127.0.0.1"
    rendered = str(address)
    return f"[{rendered}]" if address.version == 6 else rendered


def _degraded(evidence: dict[str, Any], source: str, category: str) -> None:
    row = {
        "source": re.sub(r"[^A-Za-z0-9_.-]", "_", source)[:80] or "collector",
        "category": re.sub(r"[^A-Za-z0-9_.-]", "_", category)[:40] or "unavailable",
    }
    if row not in evidence["degraded"] and len(evidence["degraded"]) < 64:
        evidence["degraded"].append(row)


def _remaining(deadline: float, clock: Callable[[], float], maximum: float) -> float:
    return max(0.001, min(maximum, deadline - clock()))


def _probe_result(value: Mapping[str, Any]) -> str:
    result = value.get("result")
    return result if result in {"ok", "timeout", "refused", "error"} else "error"


def _summarize_logs(
    log_dir: Path,
    since: float,
    deadline: float,
    evidence: dict[str, Any],
    clock: Callable[[], float],
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "retained": 0,
        "malformed": 0,
        "truncated": False,
        "channels": {},
        "slow_requests": [],
        "model_recent_error_or_slow": False,
    }
    for channel in ("requests", "events", "llm"):
        if clock() >= deadline:
            _degraded(evidence, f"logs.{channel}", "deadline")
            summary["truncated"] = True
            break
        try:
            result = read_channel(
                log_dir,
                channel,
                since_hours=max(0.0, min(float(since), 168.0)),
                limit=2_000,
                max_input_bytes=8 * 1024 * 1024,
                deadline=deadline,
            )
        except Exception:
            _degraded(evidence, f"logs.{channel}", "unavailable")
            continue
        stats = result.stats
        retained = int(getattr(stats, "retained", len(result.records)))
        malformed = int(getattr(stats, "malformed", 0))
        truncated = bool(getattr(stats, "truncated", False))
        summary["retained"] += max(0, retained)
        summary["malformed"] += max(0, malformed)
        summary["truncated"] = summary["truncated"] or truncated
        summary["channels"][channel] = {
            "retained": max(0, retained),
            "malformed": max(0, malformed),
            "truncated": truncated,
        }
        if channel == "requests":
            for row in result.records:
                if not isinstance(row, Mapping):
                    continue
                try:
                    latency = max(0, int(float(row.get("latency_ms", 0))))
                except (TypeError, ValueError, OverflowError):
                    latency = 0
                if latency < 1_000:
                    continue
                method = row.get("method")
                status = row.get("status")
                summary["slow_requests"].append({
                    "method": method if method in {"GET", "POST", "PATCH", "DELETE", "PUT"} else "unknown",
                    "path": normalize_http_path(str(row.get("path", ""))),
                    "status": int(status) if isinstance(status, int) and not isinstance(status, bool) else None,
                    "latency_ms": latency,
                })
                if len(summary["slow_requests"]) >= 32:
                    break
        elif channel == "llm":
            for row in result.records:
                if not isinstance(row, Mapping):
                    continue
                try:
                    elapsed = float(row.get("latency_ms", row.get("elapsed_ms", 0)))
                except (TypeError, ValueError, OverflowError):
                    elapsed = 0
                status = str(row.get("status", "")).lower()
                if elapsed >= 5_000 or status in {"error", "failed", "timeout"} or bool(row.get("error")):
                    summary["model_recent_error_or_slow"] = True
                    break
    return summary


def _delete_notebook(runtime: Mapping[str, Any], requested: str) -> str | None:
    if requested:
        return requested
    for row in runtime.get("active_requests", ()):
        if not isinstance(row, Mapping):
            continue
        if row.get("method") == "DELETE" and str(row.get("phase", "")).startswith("notebook_delete."):
            value = row.get("notebook_id")
            if isinstance(value, str) and value:
                return value
    return None


def _current_thread_id(dump: Any) -> int | None:
    match = re.search(r"(?m)^Current thread 0x([0-9a-fA-F]+)", str(dump))
    if match is None:
        return None
    try:
        return int(match.group(1), 16)
    except (TypeError, ValueError, OverflowError):
        return None


def _regular_file_size(path: Path) -> int | None:
    try:
        metadata = os.lstat(path)
    except OSError:
        return 0
    if not stat_module.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        return None
    return max(0, int(metadata.st_size))


def collect_incident(
    *,
    root: Path | str = ".",
    local: Path | str = "",
    pid: int | None = None,
    notebook: str = "",
    since: float = 2.0,
    output_limit_kib: int = 32,
    verbose: bool = False,
    deadline_seconds: float = MAX_INCIDENT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    """Collect all evidence against one shared monotonic deadline and render it."""

    started = clock()
    requested = max(0.001, min(float(deadline_seconds), MAX_INCIDENT_SECONDS))
    deadline = started + requested
    repo = Path(root).resolve(strict=False)
    local_dir = Path(local).resolve(strict=False) if str(local) else repo / ".local"
    diagnostics_dir = local_dir / "diagnostics"
    evidence: dict[str, Any] = {
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runtime": {},
        "runtime_fresh": False,
        "stacks": {},
        "stack_samples": [],
        "process": {},
        "health": {},
        "logs": {},
        "db": {},
        "degraded": [],
        "verbose": bool(verbose),
    }
    wal_before = _regular_file_size(local_dir / "silicon_notebook.db-wal")

    try:
        resolution = resolve_backend_pid(repo, pid, deadline=deadline, clock=clock)
    except Exception:
        resolution = {"status": "unavailable", "pid": None, "source": "operator" if pid else "auto", "candidates": []}
        _degraded(evidence, "pid", "unavailable")
    evidence["pid_resolution"] = {
        "status": resolution.get("status", "unavailable"),
        "source": resolution.get("source", "auto"),
        "candidate_count": len(resolution.get("candidates", ())) if isinstance(resolution.get("candidates"), (list, tuple)) else 0,
    }
    selected_pid = resolution.get("pid") if resolution.get("status") == "ok" else None
    identity = resolution.get("identity") if isinstance(resolution.get("identity"), Mapping) else None
    if selected_pid is None:
        _degraded(evidence, "pid", str(resolution.get("status", "unavailable")))

    runtime: Mapping[str, Any] = {}
    if selected_pid is not None and clock() < deadline:
        try:
            loaded = load_runtime_snapshot(
                diagnostics_dir / "runtime.json",
                int(selected_pid),
                deadline=deadline,
                clock=clock,
            )
        except Exception:
            loaded = {"status": "unavailable", "fresh": False, "snapshot": None}
        if loaded.get("fresh") is True and isinstance(loaded.get("snapshot"), Mapping):
            runtime = loaded["snapshot"]
            evidence["runtime"] = runtime
            evidence["runtime_fresh"] = True
            evidence["snapshot_age_seconds"] = loaded.get("age_seconds")
        else:
            _degraded(evidence, "runtime", str(loaded.get("status", "unavailable")))

    def capture_once() -> dict[str, list[str]] | None:
        if selected_pid is None or clock() >= deadline:
            return None
        try:
            capture = capture_thread_dump(
                int(selected_pid),
                diagnostics_dir,
                expected_identity=dict(identity) if identity is not None else None,
                deadline=deadline,
                clock=clock,
            )
            if capture.get("status") in {"ok", "partial"} and capture.get("dump"):
                parsed = parse_thread_dump(capture["dump"], repo)
                current = _current_thread_id(capture["dump"])
                if current is not None and "main_thread_id" not in evidence:
                    evidence["main_thread_id"] = current
                return parsed
            if capture.get("status") != "ok":
                _degraded(evidence, "stacks", str(capture.get("status", "unavailable")))
        except Exception:
            _degraded(evidence, "stacks", "unavailable")
        return None

    first_stacks = capture_once()
    if first_stacks:
        evidence["stacks"] = first_stacks
        evidence["stack_samples"].append(first_stacks)

    if selected_pid is not None and clock() < deadline:
        try:
            first_process = sample_process(
                int(selected_pid),
                expected_identity=dict(identity) if identity is not None else None,
                deadline=deadline,
                clock=clock,
            )
            evidence["process"] = first_process
            if not first_process.get("status") and clock() < deadline:
                second_process = sample_process(
                    int(selected_pid),
                    expected_identity=dict(identity) if identity is not None else None,
                    deadline=deadline,
                    clock=clock,
                )
                if not second_process.get("status"):
                    combined = dict(second_process)
                    combined["read_bytes_delta"] = max(
                        0,
                        int(second_process.get("read_bytes", 0))
                        - int(first_process.get("read_bytes", 0)),
                    )
                    combined["write_bytes_delta"] = max(
                        0,
                        int(second_process.get("write_bytes", 0))
                        - int(first_process.get("write_bytes", 0)),
                    )
                    evidence["process"] = combined
            if evidence["process"].get("status"):
                _degraded(evidence, "process", str(evidence["process"]["status"]))
        except Exception:
            evidence["process"] = {"status": "unavailable"}
            _degraded(evidence, "process", "unavailable")
    else:
        evidence["process"] = {"status": "unavailable"}

    second_stacks = capture_once()
    if second_stacks:
        evidence["stacks"] = second_stacks
        evidence["stack_samples"].append(second_stacks)

    env = _load_safe_env(repo, deadline, evidence, clock)
    backend_port = int(env.get("PORT", "8000"))
    frontend_port = int(env.get("FRONTEND_PORT", "3000"))
    host = _loopback_host(env.get("BACKEND_HOST", "127.0.0.1"))
    for role, url in (
        ("backend", f"http://{host}:{backend_port}/api/ready"),
        ("frontend", f"http://{host}:{frontend_port}/"),
    ):
        if clock() >= deadline:
            evidence["health"][role] = "unknown"
            _degraded(evidence, f"health.{role}", "deadline")
            continue
        try:
            value = probe_http(role, url, deadline=deadline, clock=clock)
            evidence["health"][role] = _probe_result(value)
        except Exception:
            evidence["health"][role] = "unknown"
            _degraded(evidence, f"health.{role}", "unavailable")

    if clock() < deadline:
        evidence["logs"] = _summarize_logs(
            local_dir / "logs", since, deadline, evidence, clock
        )
    else:
        _degraded(evidence, "logs", "deadline")

    notebook_id = _delete_notebook(runtime, str(notebook))
    if clock() < deadline:
        try:
            evidence["db"] = collect_db_evidence(
                local_dir / "silicon_notebook.db",
                notebook_id=notebook_id,
                deadline_seconds=_remaining(deadline, clock, MAX_DB_SECONDS),
            )
            db_files = evidence["db"].get("files", {})
            if (
                evidence["db"].get("evidence_complete") is True
                and wal_before is not None
                and isinstance(db_files, Mapping)
            ):
                evidence["db"]["wal_growth_bytes"] = max(
                    0, int(db_files.get("wal_bytes", 0)) - wal_before
                )
        except Exception:
            evidence["db"] = {"evidence_complete": False, "degraded": []}
            _degraded(evidence, "database", "unavailable")
    else:
        evidence["db"] = {"evidence_complete": False, "degraded": []}
        _degraded(evidence, "database", "deadline")

    if selected_pid is not None and evidence["runtime_fresh"] is True:
        try:
            if clock() >= deadline:
                raise TimeoutError
            final_runtime = load_runtime_snapshot(
                diagnostics_dir / "runtime.json",
                int(selected_pid),
                deadline=deadline,
                clock=clock,
            )
            final_snapshot = final_runtime.get("snapshot")
            stable = (
                final_runtime.get("fresh") is True
                and isinstance(final_snapshot, Mapping)
                and final_snapshot.get("state_revision") == runtime.get("state_revision")
            )
        except Exception:
            stable = False
        if not stable:
            evidence["runtime"] = {}
            evidence["runtime_fresh"] = False
            _degraded(evidence, "runtime", "raced")

    findings = diagnose(evidence)
    output_bytes = max(512, min(int(output_limit_kib) * 1024, MAX_OUTPUT_BYTES))
    return render_incident(evidence, findings, limit_bytes=output_bytes)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect one bounded, sanitized silicon-notebook incident report"
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--local", default="")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--notebook", default="")
    parser.add_argument("--since", type=float, default=2.0)
    parser.add_argument("--output-limit-kib", type=int, default=32)
    parser.add_argument("--verbose", action="store_true")
    return parser


def _main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    report = collect_incident(
        root=args.root,
        local=args.local,
        pid=args.pid,
        notebook=args.notebook,
        since=args.since,
        output_limit_kib=args.output_limit_kib,
        verbose=args.verbose,
    )
    sys.stdout.write(report)
    return 0


def main(argv=None) -> int:
    from diag_common import run_copy_safe

    return run_copy_safe(lambda: _main(argv))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "collect_incident", "main", "read_safe_env"]
