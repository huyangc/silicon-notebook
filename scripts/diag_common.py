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
from typing import Any, Dict, Iterator, Optional, Tuple

_DATED = re.compile(r"^(?P<channel>[a-z0-9_-]+)-(?P<day>\d{4}-\d{2}-\d{2})\.jsonl(?:\.gz)?$")
_ID_SEGMENT = re.compile(r"^(?:nb|src|ko|conv|user|mem|report|job)-[A-Za-z0-9_-]+$")
_TOKEN_SEGMENT = re.compile(r"^(?:shr|share|token|invite|session|auth|key)-[A-Za-z0-9_-]+$", re.I)
_SENSITIVE_ROUTE_SEGMENTS = frozenset({
    "access", "apikey", "auth", "authorization", "invite", "invites", "key", "keys",
    "password", "reset", "session", "sessions", "share", "shared", "shares", "token", "tokens",
})
_STATIC_PATH_SEGMENTS = frozenset({
    "", "api", "analytics", "answer", "ask", "cancel", "cells", "conversations", "deep-report",
    "diagnostics", "download", "events", "export", "graph", "jobs", "knowledge", "memory", "mcp",
    "notebooks", "preview", "reports", "search", "shared", "sources", "status", "stream", "tables",
})
_READ_CHUNK_BYTES = 64 * 1024
_DEFAULT_MAX_INPUT_BYTES = 64 * 1024 * 1024
_MAX_RENDERED_PATH_BYTES = 384


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


def iter_jsonl_file(path: Path, *, tail_bytes: Optional[int] = None,
                    max_input_bytes: Optional[int] = _DEFAULT_MAX_INPUT_BYTES,
                    deadline: Optional[float] = None
                    ) -> Iterator[Tuple[Optional[Dict[str, Any]], bool, int]]:
    """Yield parsed JSONL without reading or parsing beyond byte/deadline bounds.

    A negative byte count is an internal truncation sentinel consumed by
    ``read_channel``. It deliberately avoids materialising an oversized line.
    """
    limit = None if max_input_bytes is None else max(1, int(max_input_bytes))

    def raw_lines() -> Iterator[Tuple[Optional[bytes], bool]]:
        is_gzip = str(path).endswith(".gz")
        opener = gzip.open if is_gzip else open
        with opener(path, "rb") as handle:
            skip_partial = False
            if not is_gzip and tail_bytes is not None and path.stat().st_size > tail_bytes:
                handle.seek(-int(tail_bytes), 2)
                skip_partial = True
            buffered = bytearray()
            consumed = 0
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    yield None, True
                    return
                if limit is not None and consumed >= limit:
                    yield None, True
                    return
                size = _READ_CHUNK_BYTES if limit is None else min(_READ_CHUNK_BYTES, limit - consumed)
                chunk = handle.read(size)
                if not chunk:
                    if buffered and not skip_partial:
                        yield bytes(buffered), False
                    return
                consumed += len(chunk)
                if skip_partial:
                    newline = chunk.find(b"\n")
                    if newline < 0:
                        continue
                    chunk = chunk[newline + 1:]
                    skip_partial = False
                buffered.extend(chunk)
                while True:
                    newline = buffered.find(b"\n")
                    if newline < 0:
                        break
                    raw = bytes(buffered[:newline + 1])
                    del buffered[:newline + 1]
                    yield raw, False

    try:
        for raw, truncated in raw_lines():
            if truncated:
                yield None, True, -1
                return
            if raw is None:
                continue
            raw_bytes = len(raw)
            if deadline is not None and time.monotonic() >= deadline:
                yield None, True, -1
                return
            try:
                value = json.loads(raw.decode("utf-8", "replace"))
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
                 max_input_bytes: int = _DEFAULT_MAX_INPUT_BYTES,
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
            path,
            tail_bytes=None if str(path).endswith(".gz") else max_input_bytes,
            max_input_bytes=max(1, int(max_input_bytes)) - decoded_bytes,
            deadline=deadline,
        ):
            if raw_bytes < 0:
                truncated = True
                stop = True
                break
            decoded_bytes += raw_bytes
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
    redact_next = False
    for segment in clean.split("/"):
        if redact_next or _TOKEN_SEGMENT.match(segment):
            parts.append("{token}")
        elif _ID_SEGMENT.match(segment) or (len(segment) > 20 and any(ch.isdigit() for ch in segment)):
            parts.append("{id}")
        elif segment.lower() in _STATIC_PATH_SEGMENTS:
            parts.append(segment.lower())
        else:
            parts.append("{redacted}")
        redact_next = segment.lower() in _SENSITIVE_ROUTE_SEGMENTS
    normalized = "/".join(parts)
    encoded = normalized.encode("utf-8", "replace")
    if len(encoded) <= _MAX_RENDERED_PATH_BYTES:
        return normalized
    return encoded[:_MAX_RENDERED_PATH_BYTES - 3].decode("utf-8", "ignore") + "..."
