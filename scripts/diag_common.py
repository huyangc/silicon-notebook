from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import re
import sys
import time
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

_DATED = re.compile(r"^(?P<channel>[a-z0-9_-]+)-(?P<day>\d{4}-\d{2}-\d{2})\.jsonl(?:\.gz)?$")
_ID_SEGMENT = re.compile(r"^(?:nb|src|ko|conv|user|mem|report|job)-[A-Za-z0-9_-]+$")
_TOKEN_SEGMENT = re.compile(
    r"^(?:shr|rshr|share|token|invite|session|auth|key)-[A-Za-z0-9_-]+$", re.I
)
_SENSITIVE_ROUTE_SEGMENTS = frozenset({
    "access", "apikey", "auth", "authorization", "invite", "invites", "key", "keys",
    "password", "reset", "session", "sessions", "share", "shared", "shares", "token", "tokens",
})
_STATIC_PATH_SEGMENTS = frozenset({
    "", "api", "analytics", "answer", "ask", "cancel", "cells", "conversations", "deep-report",
    "diagnostics", "download", "events", "export", "graph", "jobs", "knowledge", "memory", "mcp",
    "notebooks", "preview", "public", "reports", "search", "share", "shared", "sources",
    "status", "stream", "tables",
})
# app-free 副本(批 3·W1 T-1):`diag_db.py` 只读 stdlib,不 import `app.*`
# (README.md「离线、纯 stdlib、app-free」),够不到
# `app.repositories.sqlite.access_sql.NOTEBOOK_LIVE_SQL`。这份副本必须与它逐字相等——
# 由 `test_diag_db_notebook_live_predicate_matches_access_sql`(守卫)校验,不靠约定漂移。
NOTEBOOK_LIVE_SQL = "status NOT IN ('copying','deleting')"

# 同一条谓词的 Python 侧镜像(base_recall 的挂载有效性判定是 Python 布尔表达式,
# 不是 SQL 文本——两条 AST 守卫都够不到裸的 `status != "copying"`,codex 评审实测
# 指出这是第三处真谓词)。内容必须与上面 NOTEBOOK_LIVE_SQL 的状态列表逐字一致,
# 由同一条守卫断言钉住。
NOTEBOOK_HIDDEN_STATUSES = frozenset({"copying", "deleting"})

_READ_CHUNK_BYTES = 64 * 1024
_DEFAULT_MAX_INPUT_BYTES = 64 * 1024 * 1024
_MAX_RENDERED_PATH_BYTES = 384
COPY_REPORT_LIMIT_BYTES = 32 * 1024
_CAPTURE_LIMIT_BYTES = 4 * COPY_REPORT_LIMIT_BYTES
_OPAQUE_ID = re.compile(
    r"\b(?P<prefix>nb|src|req|job|user|mem|report|ko|conv)-[A-Za-z0-9_.-]+\b",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(?i)\bBearer\s+\S+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:authorization|cookie|password|token|secret|api[_-]?key)\s*[=:]\s*\S+"
)
_SENTINEL = re.compile(r"(?i)\b(?:SENSITIVE|PRIVATE|SECRET)(?:[-_.A-Za-z0-9]*)\b")
_ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9])/(?!api(?:/|$)|docs(?:/|$)|mcp(?:/|$)|_diagnostics-test(?:/|$))[^\s)\],;]+"
)
_RELATIVE_ARTIFACT = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:backend/)?\.local(?:/[^\s)\],;]+)?|"
    r"(?<![A-Za-z0-9])(?:backend/)?\.env\b|"
    r"(?<![A-Za-z0-9])storage/[^\s)\],;]+"
)
_PSEUDONYM_LABELS = {
    "nb": "notebook",
    "src": "source",
    "req": "request",
    "job": "job",
    "user": "user",
    "mem": "memory",
    "report": "report",
    "ko": "knowledge",
    "conv": "conversation",
}
_SAFE_PSEUDONYM_LABELS = frozenset((*_PSEUDONYM_LABELS.values(), "id"))


@dataclass
class _ReportPseudonyms:
    values: dict[tuple[str, str], str]
    counters: dict[str, int]


_REPORT_PSEUDONYMS: ContextVar[Optional[_ReportPseudonyms]] = ContextVar(
    "diagnostic_report_pseudonyms", default=None
)


def pseudonym(label: str, value: Any) -> str:
    """Return a stable, non-reversible identifier scoped to one report."""

    safe_label = str(label).lower()
    if safe_label not in _SAFE_PSEUDONYM_LABELS:
        safe_label = "id"
    registry = _REPORT_PSEUDONYMS.get()
    if registry is None:
        registry = _ReportPseudonyms(values={}, counters={})
        _REPORT_PSEUDONYMS.set(registry)
    key = (safe_label, str(value))
    rendered = registry.values.get(key)
    if rendered is None:
        registry.counters[safe_label] = registry.counters.get(safe_label, 0) + 1
        rendered = f"{safe_label}#{registry.counters[safe_label]}"
        registry.values[key] = rendered
    return rendered


class _BoundedCapture(io.TextIOBase):
    def __init__(self, maximum: int = _CAPTURE_LIMIT_BYTES) -> None:
        self._maximum = maximum
        self._parts: list[str] = []
        self._used = 0
        self.truncated = False

    @property
    def encoding(self) -> str:
        return "utf-8"

    def write(self, value: str) -> int:
        text = str(value)
        encoded = text.encode("utf-8", "replace")
        remaining = self._maximum - self._used
        if remaining > 0:
            kept = encoded[:remaining].decode("utf-8", "ignore")
            self._parts.append(kept)
            self._used += len(kept.encode("utf-8"))
        if len(encoded) > remaining:
            self.truncated = True
        return len(text)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return False

    def getvalue(self) -> str:
        value = "".join(self._parts)
        return value + ("\n[capture_truncated=true]\n" if self.truncated else "")


def finite_number(
    value: Any,
    *,
    minimum: float = 0.0,
    maximum: float = 10**15,
) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < minimum or number > maximum:
        return None
    return number


def sanitize_copy_text(value: Any) -> str:
    """Fail-closed final scrub for text already selected by an engine."""

    text = str(value).replace("\x00", "").replace("\r", "\n")

    def replace_identifier(match: re.Match[str]) -> str:
        raw = match.group(0)
        prefix = match.group("prefix").lower()
        label = _PSEUDONYM_LABELS.get(prefix, "id")
        return pseudonym(label, raw)

    text = _BEARER.sub("<auth>", text)
    text = _SECRET_ASSIGNMENT.sub("<redacted>", text)
    text = _OPAQUE_ID.sub(replace_identifier, text)
    text = _SENTINEL.sub("<redacted>", text)
    text = _RELATIVE_ARTIFACT.sub("<artifact>", text)
    text = _ABSOLUTE_PATH.sub("<path>", text)
    return text


def bound_copy_text(value: Any, limit_bytes: int = COPY_REPORT_LIMIT_BYTES) -> str:
    limit = max(1, min(int(limit_bytes), COPY_REPORT_LIMIT_BYTES))
    text = sanitize_copy_text(value)
    if not text.endswith("\n"):
        text += "\n"
    encoded = text.encode("utf-8", "strict")
    if len(encoded) <= limit:
        return text
    marker = "[output_truncated=true]\n"
    budget = max(0, limit - len(marker.encode("utf-8")))
    prefix = encoded[:budget].decode("utf-8", "ignore")
    newline = prefix.rfind("\n")
    if newline >= 0:
        prefix = prefix[: newline + 1]
    return prefix + marker


def run_copy_safe(call, *, limit_bytes: int = COPY_REPORT_LIMIT_BYTES) -> int:
    """Run one engine behind a global stdout/stderr privacy and byte boundary."""

    registry_token = _REPORT_PSEUDONYMS.set(_ReportPseudonyms(values={}, counters={}))
    real_stdout, real_stderr = sys.stdout, sys.stderr
    stdout_capture = _BoundedCapture()
    stderr_capture = _BoundedCapture()
    sys.stdout, sys.stderr = stdout_capture, stderr_capture
    result = 0
    invalid_arguments = False
    try:
        result = int(call() or 0)
    except SystemExit as exc:
        try:
            result = int(exc.code or 0)
        except (TypeError, ValueError, OverflowError):
            result = 2
        invalid_arguments = result != 0
    except BaseException:
        stdout_capture = _BoundedCapture()
        stdout_capture.write("diagnostic_error=unavailable\n")
        stderr_capture = _BoundedCapture()
        result = 0
    finally:
        sys.stdout, sys.stderr = real_stdout, real_stderr

    if invalid_arguments:
        payload = "diagnostic_error=invalid_arguments\n"
    else:
        payload = stdout_capture.getvalue()
        if stderr_capture.getvalue():
            payload += "\n" + stderr_capture.getvalue()
    try:
        real_stdout.write(bound_copy_text(payload, limit_bytes))
        real_stdout.flush()
        return result
    finally:
        _REPORT_PSEUDONYMS.reset(registry_token)


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
    # Identity for "the same log line seen twice" (a rotated day file and its
    # own .gz archive both being present). Every field a record actually
    # carries must feed the key, or a whole event kind collapses into a single
    # retained row: db_write_lock_slow / db_write_lock_stats carry none of the
    # ask/http fields below, so before `site`/`wait_ms`/`hold_ms` were included
    # every write-lock event in a file hashed identically and all but the first
    # were dropped as "duplicates". Adding fields can only ever split keys
    # apart, never merge them, so no existing channel's dedup gets weaker.
    stable = [
        channel,
        str(record.get("id", "")),
        str(record.get("ts", "")),
        str(record.get("kind", "")),
        str(record.get("stage", "")),
        str(record.get("method", "")),
        str(record.get("path", "")),
        str(record.get("latency_ms", "")),
        str(record.get("site", "")),
        str(record.get("wait_ms", "")),
        str(record.get("hold_ms", "")),
    ]
    return hashlib.sha256("\x1f".join(stable).encode("utf-8", "replace")).hexdigest()


def read_channel(log_dir: Path, channel: str, *, since_hours: Optional[float] = None,
                 limit: int = 50000, now: Optional[datetime] = None,
                 explicit: Optional[Path] = None,
                 max_input_bytes: Optional[int] = _DEFAULT_MAX_INPUT_BYTES,
                 deadline: Optional[float] = None) -> ChannelRecords:
    discovered = discover_channel_files(Path(log_dir), channel, explicit)
    cutoff = None if since_hours is None else (now or datetime.now()).timestamp() - since_hours * 3600
    cutoff_day = None if cutoff is None else datetime.fromtimestamp(cutoff).strftime("%Y-%m-%d")
    candidates = tuple(
        path for path in discovered
        if cutoff_day is None
        or not (match := _DATED.match(path.name))
        or match.group("day") >= cutoff_day
    )
    byte_limit = None if max_input_bytes is None else max(1, int(max_input_bytes))
    selected = []
    selected_bytes = 0
    oversized = False
    if byte_limit is None:
        selected.extend(candidates)
    else:
        for path in reversed(candidates):
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            if selected and selected_bytes + size > byte_limit:
                break
            selected.append(path)
            oversized = oversized or size > byte_limit
            selected_bytes += min(size, byte_limit)
            if selected_bytes >= byte_limit:
                break
    paths = tuple(sorted(selected, key=lambda path: candidates.index(path)))
    newest_files_first = since_hours is not None
    if newest_files_first:
        paths = tuple(reversed(paths))
    retained = deque(maxlen=max(1, int(limit)))
    seen = set()
    parsed = matched = malformed = duplicates = 0
    truncated = len(paths) < len(candidates) or oversized
    decoded_bytes = 0
    stop = False
    for path in paths:
        path_retained = deque(maxlen=retained.maxlen) if newest_files_first else retained
        for record, bad, raw_bytes in iter_jsonl_file(
            path,
            tail_bytes=None if str(path).endswith(".gz") else byte_limit,
            max_input_bytes=None if byte_limit is None else byte_limit - decoded_bytes,
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
            path_retained.append(record)
            if len(seen) > max(2 * int(limit), 1000):
                seen = {_record_key(channel, row) for row in retained}
                seen.update(_record_key(channel, row) for row in path_retained)
        if newest_files_first:
            room = retained.maxlen - len(retained)
            for record in reversed(tuple(path_retained)[-room:] if room else ()):
                retained.appendleft(record)
        if stop:
            break
    truncated = truncated or matched - duplicates > len(retained)
    return ChannelRecords(
        tuple(retained),
        ScanStats(len(paths), parsed, matched, malformed, duplicates, len(retained), truncated),
    )


@dataclass(frozen=True)
class DatabaseTarget:
    """Which database the deployment actually serves from.

    The diagnostics used to open ``.local/silicon_notebook.db`` unconditionally.
    On a PostgreSQL deployment that file is stale or empty, so every query
    silently answered from the wrong database — worse than refusing, because the
    output looks like a real diagnosis.  Resolution mirrors the service: the
    process environment wins over ``.env``, matching pydantic-settings.
    """

    backend: str                      # "sqlite" | "postgres" | "unknown"
    sqlite_path: Optional[str] = None
    url_scheme: str = ""              # 脱敏:只留 scheme,绝不带凭据/host
    source: str = "default"           # "env" | "dotenv" | "default"

    @property
    def is_sqlite(self) -> bool:
        return self.backend == "sqlite"

    def explain(self) -> str:
        if self.is_sqlite:
            return f"SQLite（来源:{self.source}）"
        if self.backend == "postgres":
            return f"PostgreSQL（DATABASE_URL scheme={self.url_scheme}，来源:{self.source}）"
        return f"未知后端（scheme={self.url_scheme or '?'}，来源:{self.source}）"

    def skip_note(self, what: str = "本段") -> str:
        return (
            f"({what}目前只支持 SQLite;当前部署是 {self.explain()} — "
            f"跳过而不是读取可能陈旧的 .local/silicon_notebook.db)"
        )

    def resolve_sqlite_file(self, fallback_dir: str) -> Optional[str]:
        """The SQLite file this deployment actually serves from.

        Confirming the *backend* is not enough: a valid non-default URL such as
        ``sqlite:///data/production.db`` still leaves the fixed
        ``<local_dir>/silicon_notebook.db`` pointing at a stale file.  Falls
        back to the conventional location only when the URL names no file.
        """
        import os as _os

        if not self.is_sqlite:
            return None
        if self.sqlite_path:
            return self.sqlite_path
        return _os.path.join(fallback_dir, "silicon_notebook.db")

    def sqlite_readonly_uri(self, fallback_dir: str) -> Optional[str]:
        """The read-only URI for that file, safe to hand to ``sqlite3.connect``.

        A configured query string is part of the filename the service opens, so
        the resolved path can legitimately contain ``?``.  Interpolating it raw
        into ``file:{path}?mode=ro`` makes SQLite parse that suffix as URI
        parameters (``mode=ro?mode=ro``) instead of opening the literal file, so
        the name has to be percent-encoded.  Sole construction point.
        """
        from urllib.parse import quote as _quote

        path = self.resolve_sqlite_file(fallback_dir)
        return f"file:{_quote(path)}?mode=ro" if path else None


def _authoritative_dotenv_values(path: Path) -> Optional[Dict[str, Any]]:
    """Parse with python-dotenv itself when it is importable.

    The application reads `.env` through pydantic-settings, i.e. python-dotenv,
    which also expands `${VAR}` interpolation.  Re-implementing that here would
    be a third mirror to drift against, so use the real parser whenever the
    backend dependencies are installed — the usual case on a machine that runs
    the service.  These scripts must stay stdlib-only, hence the fallback below
    rather than a hard dependency.
    """
    try:
        from dotenv import dotenv_values as _dotenv_values
    except Exception:
        return None
    try:
        return {
            str(key): value for key, value in _dotenv_values(str(path)).items()
            if key
        }
    except Exception:
        return None


def _dotenv_database_url(path: Path) -> Optional[str]:
    values = _authoritative_dotenv_values(path)
    if values is not None:
        found: Optional[str] = None
        # File order, not sorted order: with case variants of the same key
        # pydantic-settings takes the later declaration, while sorting would
        # let the lowercase spelling win regardless of where it appears.
        for key, value in values.items():
            if key.upper() == "DATABASE_URL":
                found = value
        return None if found is None else str(found)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    value: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, rest = line.partition("=")
        key = key.strip()
        # `export DATABASE_URL=...` is a supported form: the migration
        # activation path writes it and the application's dotenv loader accepts
        # it.  Missing the prefix here silently falls back to SQLite, which is
        # exactly the stale-database misread this resolver exists to prevent.
        if key.startswith("export ") or key.startswith("export\t"):
            key = key[len("export"):].strip()
        # The application's Settings uses `case_sensitive=False`, so
        # `database_url=` is a live spelling too.
        if key.upper() != "DATABASE_URL":
            continue
        value = _dotenv_value(rest)   # 后出现的覆盖先出现的,与 dotenv 一致
    return value


def _dotenv_value(raw: str) -> str:
    """Unquote and strip an inline comment the way python-dotenv does.

    ``DATABASE_URL=sqlite:///.local/prod.db # production`` is valid dotenv and
    the application loads it without the trailing comment.  Keeping the comment
    here turns the resolved path into a file that does not exist.  Inside
    quotes a ``#`` is content, not a comment.
    """
    rest = raw.strip()
    if rest[:1] in ('"', "'"):
        quote = rest[0]
        end = rest.find(quote, 1)
        return rest[1:end] if end > 0 else rest[1:]
    if rest.startswith("#"):
        return ""
    marker = rest.find(" #")
    if marker >= 0:
        rest = rest[:marker]
    marker = rest.find("\t#")
    if marker >= 0:
        rest = rest[:marker]
    return rest.strip()


def _environ_case_insensitive(name: str) -> Optional[str]:
    """Look up a process variable the way ``Settings(case_sensitive=False)`` does.

    An exact hit wins; otherwise the first case-insensitive match in a stable
    order, so two spellings in one environment resolve deterministically.
    """
    import os as _os

    # pydantic-settings folds the environment into a lowercased mapping, so a
    # later entry overwrites an earlier one regardless of spelling.  Giving the
    # uppercase name priority would pick the other value when both exist.
    wanted = name.lower()
    found: Optional[str] = None
    for key, value in _os.environ.items():
        if key.lower() == wanted:
            found = value
    return found


def _env_file_for(root: str) -> Optional[Path]:
    """Mirror ``app.core.config``: the override wins, empty means read nothing."""
    import os as _os

    # Exact lookup on purpose: `app.core.config` reads this bootstrap variable
    # with a plain `os.environ.get` before Settings exists, so its
    # case-insensitivity does not apply.  Being more permissive here would
    # resolve a different backend than the service actually uses.
    override = _os.environ.get("SILICON_NOTEBOOK_ENV_FILE")
    if override is None:
        return Path(root) / ".env"
    override = override.strip()
    return Path(override) if override else None


def _core_database_url_module():
    """Load the isolated core URL parser without importing the app package.

    Hand-mirroring this parsing is what kept diverging from the service: URL
    queries, malformed DSNs, and scheme normalization each have a rule here
    already.  `backend/app/core/database_url.py` deliberately has no package
    imports, so the diagnostics can reuse the authority instead of guessing.
    """
    import importlib.util as _importlib_util

    path = (
        Path(__file__).resolve().parent.parent
        / "backend" / "app" / "core" / "database_url.py"
    )
    try:
        spec = _importlib_util.spec_from_file_location(
            "silicon_notebook_diag_database_url", path
        )
        if spec is None or spec.loader is None:
            return None
        module = _importlib_util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:      # 诊断不能因为加载失败而改口径
        return None


def resolve_database_target(root: str) -> DatabaseTarget:
    """Resolve the serving database from DATABASE_URL, never by assumption."""
    import os as _os

    # Presence wins, not truthiness: `Settings` gives an explicitly present but
    # blank DATABASE_URL precedence and rejects it, so the service cannot start.
    # Falling through to .env or the SQLite default here would diagnose a
    # configuration the service is not running on.
    raw = _environ_case_insensitive("DATABASE_URL")
    source = "env"
    if raw is None:
        env_file = _env_file_for(root)
        raw = _dotenv_database_url(env_file) if env_file is not None else None
        source = "dotenv" if raw is not None else "default"
    if raw is None:
        return DatabaseTarget(backend="sqlite", source="default")
    url = str(raw).strip()
    if not url:
        return DatabaseTarget(backend="unknown", source=source)
    core = _core_database_url_module()
    if core is None:
        return DatabaseTarget(backend="unknown", source=source)
    try:
        identity = core.database_identity(url)
    except Exception:
        # Malformed URLs are rejected by the service too.  Keep nothing from the
        # raw value: a malformed DSN can carry credentials, and this output is
        # meant to be pasted into a report.
        return DatabaseTarget(backend="unknown", source=source)
    if identity.scheme == "sqlite":
        # `identity.database` keeps any query string, exactly as the service's
        # `Settings.sqlite_path` does — diagnosing a different file than the one
        # actually opened is the whole failure mode being fixed.
        path = str(identity.database or "")
        resolved = (
            None if not path
            else path if _os.path.isabs(path)
            else _os.path.join(_os.path.abspath(root), path)
        )
        return DatabaseTarget(
            backend="sqlite", sqlite_path=resolved,
            url_scheme="sqlite", source=source,
        )
    return DatabaseTarget(
        backend="postgres", url_scheme=identity.scheme, source=source
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
