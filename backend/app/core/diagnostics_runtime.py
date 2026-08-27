"""Bounded, metadata-only process diagnostics for live incident inspection.

The runtime is deliberately independent from FastAPI and the repository layer.
Call sites use the module-level helpers, which become no-ops unless one runtime
is installed for the process.
"""

from __future__ import annotations

import contextvars
import errno
import faulthandler
import hashlib
import itertools
import json
import math
import os
import re
import secrets
import signal
import stat
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional


SCHEMA_VERSION = 1
_MAX_ACTIVE = 1_000
_MAX_RECENT_JOBS = 100
_MAX_IDENTIFIER_LENGTH = 200
_MAX_ROUTE_SEGMENTS = 16
_MAX_CONCURRENCY_VALUE = 1_000_000
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NOTEBOOK_ROUTE = re.compile(r"^/api/notebooks/([^/?#]+)(?:/(.*))?$")
_OPAQUE_NOTEBOOK_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")
_NOTEBOOK_ROUTE_TEMPLATES: tuple[tuple[Optional[str], ...], ...] = (
    (),
    ("analytics",),
    ("analytics", "content-overview"),
    ("checkup",),
    ("memories",),
    ("memories", "from-answer"),
    ("answer-memory-links",),
    ("sources",),
    ("sources", "import"),
    ("sources", "url"),
    ("sources", "reparse"),
    ("sources", None),
    ("sources", None, "elements"),
    ("sources", None, "elements-page"),
    # 命令目录抽取(方案 C·C1b)。第三段之后全是固定字面量,来源 id 走 None 打
    # {id},所以整条路径可以原样进诊断快照——job_id / state / cursor 都在查询串里。
    ("sources", None, "command-catalog"),
    ("sources", None, "command-catalog", "preview"),
    ("sources", None, "command-catalog", "job"),
    ("sources", None, "command-catalog", "cancel"),
    ("sources", None, "command-catalog", "candidates"),
    ("sources", None, "command-catalog", "apply"),
    ("sources", None, "command-catalog", "dismiss"),
    ("backfill-vectors",),
    ("assets",),
    ("assets", None),
    ("knowledge-types",),
    ("knowledge",),
    ("knowledge", None),
    ("knowledge", None, "merge"),
    ("knowledge", None, "promote"),
    ("duplicates",),
    ("graph",),
    ("search",),
    ("ask",),
    ("ask", "intent"),
    ("ask", "intent", "stream"),
    ("ask", "stream"),
    ("ask", "jobs", None),
    ("ask", "jobs", None, "cancel"),
    ("conversations",),
    # 会话公开分享的发放/回读/撤销(T2)。第二段 conversation_id 走 None 打 {id},
    # 第三段 share 是固定字面量;token 只出现在匿名 /api/public/conversations/{token}
    # (T3,另一张模板表),不进这条路径。少了它整条会落到兜底 {redacted},丢观测。
    ("conversations", None, "share"),
    ("edge-review-queue",),
    ("relations", None, "review"),
    ("object-schemas",),
    ("object-schemas", None),
    ("tier",),
    ("bases",),
    ("mountable",),
    ("mounted-by-count",),
    ("share",),
    ("membership",),
    # Agentic Memory P1(T6)。第二段是固定字面量,label 走 None 打 {id},scope
    # 只在查询串/请求体里,所以整条路径可以原样进诊断快照。⚠️ 顺序敏感(同上面
    # knowhow history/diff 的注释):字面末段 "rebuild" 必须排在通配 None 末段
    # 之前,否则 None 先吞掉 rebuild,把它误归一成 understanding/{id}。
    ("understanding",),
    ("understanding", "rebuild"),
    ("understanding", None),
    # Agent 观察管理(Agentic Memory P3-T5)。固定字面量单段:GET 的 limit 与
    # DELETE 的 agent_profile_id 都在查询串里,路径本身无用户内容,整条可原样
    # 进诊断快照;少了这条会落到兜底 {redacted},丢观测。
    ("agent-observations",),
    # 群组授权边(群组知识共享 P1-T3)。路径里没有任何用户内容:第二段是固定字面量,
    # 授权边 id 走 None 打 {id},主体 id / 角色都在请求体里,所以整条路径可以原样
    # 进诊断快照。
    ("grants",),
    ("grants", None),
    # 成员贡献审批流(群组知识共享 P2-T3)。同 grants:第二段是固定字面量,申请 id 走
    # None 打 {id},group_id 在请求体里。少了这两条整条会落到兜底 {redacted},丢观测。
    ("share-requests",),
    ("share-requests", None),
    ("kg", "search"),
    ("kg", "build"),
    ("kg", "rebuild"),
    ("kg", "relink"),
    ("kg", "relink", "status"),
    ("kg", "conflicts", "resolve"),
    ("kg", "conflicts", "pending"),
    ("kg", "conflicts", None, "confirm"),
    ("kg", "conflicts", None, "reject"),
    # KG 质量分析视图(T3)的两个只读端点。路径里没有任何用户内容(第二段是固定的
    # 字面量,分页/上限走查询串),所以整条路径可以原样进诊断快照,不必打 {redacted}。
    ("kg-analysis",),
    ("kg-analysis", "sources"),
    ("reports",),
    ("reports", "export"),
    ("reports", None),
    ("reports", None, "intent"),
    ("reports", None, "outline"),
    ("reports", None, "generate"),
    ("reports", None, "cancel"),
    ("indexing-pipeline",),
    # 公开分享链接的发布/撤销。第三段之后是固定字面量，token 只出现在
    # `/api/public/reports/{token}`（另一张模板表），不进这条路径。
    ("reports", None, "share"),
    ("unified-kg",),
    ("unified-kg", "rebuild"),
    ("unified-kg", "rebuild", "status"),
    ("unified-kg", "status"),
    ("unified-kg", "pending-merges"),
    ("unified-kg", "merges", "review"),
    ("unified-kg", "merges", "review-all"),
    ("unified-kg", "merges", "review-job"),
    ("unified-kg", "merges", None, "confirm"),
    ("unified-kg", "merges", None, "reject"),
    ("scale-index", "rebuild"),
    ("scale-index", "cancel"),
    ("scale-index", "status"),
    ("index-status",),
    ("concepts", None, "detail"),
    ("objects", None, "context"),
    ("objects", None, "neighbors"),
    ("schema-proposals",),
    ("paper-meta", "backfill"),
    ("knowhow",),
    ("knowhow", "import"),
    ("knowhow", "import", "preview"),
    ("knowhow", None),
    ("knowhow", None, "reproject"),
    ("knowhow", None, "columns"),
    ("knowhow", None, "columns", None),
    ("knowhow", None, "rows"),
    ("knowhow", None, "rows", None),
    ("knowhow", None, "rows", None, "complete"),
    ("knowhow", None, "rows", None, "complete", "stream"),
    ("knowhow", None, "rows", None, "cells", None),
    ("knowhow", None, "rows", None, "cells", None, "optimize"),
    ("knowhow", None, "rows", None, "cells", None, "optimize", "stream"),
    ("knowhow", None, "rows", None, "cells", None, "reformat"),
    ("knowhow", None, "rows", None, "cells", None, "reformat", "stream"),
    ("knowhow", None, "rows", None, "cells", None, "history"),
    ("knowhow", None, "cells"),
    ("knowhow", None, "template"),
    ("knowhow", None, "append"),
    ("knowhow", None, "transfer"),
    ("knowhow", None, "history"),
    # ⚠️ 顺序敏感：_request_metadata 取**首个**同长匹配。同为 4 段的
    # history/diff、history/prune（字面末段）必须排在 history/{seq}（通配
    # None 末段）之前，否则 None 会先吞掉 diff/prune，把它们误归一成
    # history/{id}。这是本白名单里其中一处「同长同前缀、末段一字面一通配」的
    # 情形（另一处见上面 understanding/rebuild 的注释）。
    ("knowhow", None, "history", "diff"),
    ("knowhow", None, "history", "prune"),
    ("knowhow", None, "history", None),
    ("knowhow", None, "revert"),
    ("knowhow", None, "milestones"),
    ("knowhow", None, "milestones", None),
)
_ROUTE_TEMPLATES: tuple[tuple[Optional[str], ...], ...] = (
    ("api", "health"),
    ("api", "files", None),
    ("api", "search", None),
    ("api", "shared", None),
    # 公开分享的报告：token 是全部授权，所以它按不透明 id 打码，路径本身可见。
    ("api", "public", "reports", None),
    # 会话公开分享的匿名读页与资产字节(T3/T4)。token / alias 是全部授权,按不透明
    # id 打码,路径本身可见——镜像上面的 public/reports 模板。少了它们,两条匿名端点会
    # 落到兜底的 /api/{id}/{id}/{id}(资产路径再多两段),丢观测:慢的匿名读页与慢的
    # 资产取字节在诊断里分不开。
    ("api", "public", "conversations", None),
    ("api", "public", "conversations", None, "assets", None),
    ("api", "sources", None),
    ("api", "sources", None, "elements-page"),
    ("api", "sources", None, "elements", None),
    ("api", "memory", None),
    ("api", "conversations", None),
    ("api", "answers", None, "memory-preview"),
    ("api", "answers", None, "memory-preview", "stream"),
    ("api", "agent", "knowhow", "tables", None),
    ("api", "agent", "knowhow", "tables", None, "rows", None),
    (
        "api",
        "agent",
        "knowhow",
        "tables",
        None,
        "rows",
        None,
        "cells",
        None,
    ),
    ("api", "kg", "concept-whitelist", None),
    # 群组与授权边(群组知识共享 P1-T3)。路径段全是固定字面量 + 不透明 id,组名/
    # 角色/用户名都在请求体或查询串里,所以整条路径可以原样进诊断快照。
    # 少了这几条,它们会落到兜底的 `/api/{id}/{id}`——不是泄露,是**丢观测**:整个
    # 群组域在诊断里挤成同一个桶,慢请求分不出是哪个端点。
    ("api", "groups"),
    ("api", "groups", None),
    ("api", "groups", None, "members", None),
    ("api", "groups", None, "membership"),
    ("api", "groups", None, "shared-notebooks"),
    ("api", "groups", None, "shared-notebooks", None),
    # 组管理员的审批面(群组知识共享 P2-T3)。申请 id 走 None 打 {id},approve/reject 是
    # 固定末段字面量;整条路径无用户内容。
    ("api", "groups", None, "share-requests"),
    ("api", "groups", None, "share-requests", None, "approve"),
    ("api", "groups", None, "share-requests", None, "reject"),
    # 用户名在**查询串**里(`?username=`),不在路径上——路径本身没有用户内容。
    ("api", "users", "resolve"),
    ("_diagnostics-test", "block"),
    ("docs",),
    ("openapi.json",),
    ("mcp",),
)
_CONCURRENCY_GROUPS = frozenset({"kg", "llm", "embedding", "rerank"})
_CONCURRENCY_FIELDS = frozenset(
    {
        "active",
        "maximum",
        "waiting",
        "window_active",
        "window_max",
        "window_waiting",
        "job_active",
        "job_max",
        "job_waiting",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SqlMetadata:
    verb: str
    table: str
    fingerprint: str


def normalize_sql_metadata(sql: str | SqlMetadata) -> SqlMetadata:
    if isinstance(sql, SqlMetadata):
        return sql
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
        fingerprint=hashlib.sha256(
            scrubbed.upper().encode("utf-8", "replace")
        ).hexdigest()[:12],
    )


@dataclass(frozen=True)
class _DiagnosticContext:
    request_id: Optional[str] = None
    job_id: Optional[str] = None
    notebook_id: Optional[str] = None
    phase: Optional[str] = None


_diagnostic_context: contextvars.ContextVar[_DiagnosticContext] = (
    contextvars.ContextVar("diagnostic_context", default=_DiagnosticContext())
)
_runtime_lock = threading.Lock()
_installed_runtime: Optional["DiagnosticsRuntime"] = None
_installed_runtime_depth = 0
_signal_owner_lock = threading.Lock()
_signal_owner: Optional["DiagnosticsRuntime"] = None


class _UnsafeDiagnosticsArtifact(Exception):
    """An artifact path failed the private, single-link ownership contract."""


def _context_fields(context: _DiagnosticContext) -> dict[str, Optional[str]]:
    return {
        "request_id": context.request_id,
        "job_id": context.job_id,
        "notebook_id": context.notebook_id,
        "phase": context.phase,
    }


def _bounded_text(value: Any, maximum: int = _MAX_IDENTIFIER_LENGTH) -> str:
    return str(value)[:maximum]


def _request_metadata(path: str) -> tuple[str, Optional[str]]:
    path_only = str(path).split("?", 1)[0].split("#", 1)[0]
    if path_only in {"/api/notebooks", "/api/notebooks/shared-by-me"}:
        return path_only, None
    match = _NOTEBOOK_ROUTE.match(path_only)
    if match is not None:
        candidate = match.group(1)
        notebook_id = candidate if _OPAQUE_NOTEBOOK_ID.fullmatch(candidate) else None
        raw_suffix = match.group(2)
        suffix = () if not raw_suffix else tuple(raw_suffix.split("/"))
        for template in _NOTEBOOK_ROUTE_TEMPLATES:
            if len(template) != len(suffix):
                continue
            if all(
                expected is None or expected == actual
                for expected, actual in zip(template, suffix)
            ):
                normalized_suffix = tuple(
                    expected if expected is not None else "{id}"
                    for expected in template
                )
                return "/" + "/".join(
                    ("api", "notebooks", "{id}", *normalized_suffix)
                ), notebook_id
        return "/api/notebooks/{id}/{redacted}", notebook_id
    segments = tuple(
        segment for segment in path_only.split("/", _MAX_ROUTE_SEGMENTS) if segment
    )
    for template in _ROUTE_TEMPLATES:
        if len(template) != len(segments):
            continue
        if all(expected is None or expected == actual for expected, actual in zip(template, segments)):
            normalized = [
                expected if expected is not None else "{id}" for expected in template
            ]
            return "/" + "/".join(normalized), None
    normalized = ["api", *("{id}" for _ in segments[1:])] if segments[:1] == ("api",) else ["{id}" for _ in segments]
    return "/" + "/".join(normalized), None


def _finite_concurrency_value(value: Any) -> Optional[int | float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value < 0 or value > _MAX_CONCURRENCY_VALUE:
        return None
    return value


def _normalize_concurrency(value: Any) -> dict[str, dict[str, int | float]]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, int | float]] = {}
    for group in _CONCURRENCY_GROUPS:
        raw_group = value.get(group)
        if not isinstance(raw_group, dict):
            continue
        normalized: dict[str, int | float] = {}
        for field in _CONCURRENCY_FIELDS:
            number = _finite_concurrency_value(raw_group.get(field))
            if number is not None:
                normalized[field] = number
        if normalized:
            result[group] = normalized
    return result


class DiagnosticsRuntime:
    """Own the bounded registries and heartbeat files for one process."""

    def __init__(
        self,
        diagnostics_dir: Path,
        readiness_provider: Callable[[], dict[str, Any]],
        concurrency_provider: Callable[[], dict[str, Any]],
        *,
        interval_seconds: float = 2.0,
        enable_signal: bool = True,
    ) -> None:
        self.diagnostics_dir = Path(diagnostics_dir)
        self._readiness_provider = readiness_provider
        self._concurrency_provider = concurrency_provider
        self._interval_seconds = max(0.001, float(interval_seconds))
        self._enable_signal = bool(enable_signal)

        self._lock = threading.Lock()
        self._active_requests: dict[int, dict[str, Any]] = {}
        self._active_sql: dict[int, dict[str, Any]] = {}
        self._active_jobs: dict[int, dict[str, Any]] = {}
        self._write_waiters: dict[int, dict[str, Any]] = {}
        self._write_holder: Optional[dict[str, Any]] = None
        self._recent_jobs: deque[dict[str, Any]] = deque(maxlen=_MAX_RECENT_JOBS)
        self._ids = itertools.count(1)

        started = _utc_now()
        self._process_started_at = started
        self._last_state_change_at = started
        self._state_revision = 1
        self._snapshot_failures = 0

        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._dump_handle: Any = None
        self._directory_fd: Optional[int] = None
        self._directory_identity: Optional[tuple[int, int]] = None
        self._owns_signal = False
        self.signal_capture_available = False
        self._lifecycle_state = "new"

    def _changed_locked(self) -> None:
        self._state_revision += 1
        self._last_state_change_at = _utc_now()
        self._wake.set()

    def _snapshot_failed(self, count: int = 1) -> None:
        with self._lock:
            self._snapshot_failed_locked(count)

    def _snapshot_failed_locked(self, count: int = 1) -> None:
        self._snapshot_failures += count
        self._state_revision += 1
        self._last_state_change_at = _utc_now()

    def _bounded_add_locked(
        self, registry: dict[int, dict[str, Any]], entry: dict[str, Any]
    ) -> Optional[int]:
        if len(registry) >= _MAX_ACTIVE:
            return None
        identifier = next(self._ids)
        registry[identifier] = entry
        self._changed_locked()
        return identifier

    @staticmethod
    def _active_entry(**values: Any) -> dict[str, Any]:
        return {
            **values,
            "thread_id": threading.get_ident(),
            "started_at": _utc_now(),
            "_started_monotonic": time.monotonic(),
        }

    def _enter_request(
        self, request_id: str, method: str, path: str
    ) -> tuple[Optional[int], contextvars.Token[_DiagnosticContext]]:
        normalized_path, notebook_id = _request_metadata(path)
        previous = _diagnostic_context.get()
        context = _DiagnosticContext(
            request_id=_bounded_text(request_id),
            job_id=previous.job_id,
            notebook_id=notebook_id,
            phase=previous.phase,
        )
        context_token = _diagnostic_context.set(context)
        entry = self._active_entry(
            method=_bounded_text(method, 16).upper(),
            path=normalized_path,
            **_context_fields(context),
        )
        with self._lock:
            token = self._bounded_add_locked(self._active_requests, entry)
        return token, context_token

    def _exit_request(
        self,
        token: Optional[int],
        context_token: contextvars.Token[_DiagnosticContext],
    ) -> None:
        try:
            if token is not None:
                with self._lock:
                    if self._active_requests.pop(token, None) is not None:
                        self._changed_locked()
        finally:
            _diagnostic_context.reset(context_token)

    def _enter_phase(
        self, phase: str
    ) -> tuple[
        contextvars.Token[_DiagnosticContext],
        list[tuple[Optional[dict[int, dict[str, Any]]], int, dict[str, Any], object]],
    ]:
        previous = _diagnostic_context.get()
        context = _DiagnosticContext(
            request_id=previous.request_id,
            job_id=previous.job_id,
            notebook_id=previous.notebook_id,
            phase=_bounded_text(phase, 160),
        )
        context_token = _diagnostic_context.set(context)
        changed: list[
            tuple[Optional[dict[int, dict[str, Any]]], int, dict[str, Any], object]
        ] = []
        thread_id = threading.get_ident()
        with self._lock:
            for registry in (
                self._active_requests,
                self._active_jobs,
                self._active_sql,
                self._write_waiters,
            ):
                for identifier, entry in registry.items():
                    request_handoff = (
                        registry is self._active_requests
                        and context.request_id is not None
                        and context.job_id is None
                        and entry.get("request_id") == context.request_id
                    )
                    same_thread = entry["thread_id"] == thread_id
                    if not request_handoff and not same_thread:
                        continue
                    if (
                        context.request_id is not None
                        and entry.get("request_id") != context.request_id
                    ):
                        continue
                    if (
                        context.job_id is not None
                        and entry.get("job_id") != context.job_id
                    ):
                        continue
                    owner = object()
                    self._push_phase_overlay(entry, owner, context.phase, thread_id)
                    changed.append((registry, identifier, entry, owner))
            if (
                self._write_holder is not None
                and self._write_holder["thread_id"] == thread_id
            ):
                holder = self._write_holder
                owner = object()
                self._push_phase_overlay(holder, owner, context.phase, thread_id)
                changed.append((None, -1, holder, owner))
            if changed:
                self._changed_locked()
        return context_token, changed

    @staticmethod
    def _push_phase_overlay(
        entry: dict[str, Any], owner: object, phase: Optional[str], thread_id: int
    ) -> None:
        overlays = entry.get("_phase_overlays")
        if not isinstance(overlays, list):
            entry["_phase_base"] = (entry.get("phase"), entry["thread_id"])
            overlays = []
            entry["_phase_overlays"] = overlays
        overlays.append((owner, phase, thread_id))
        entry["phase"] = phase
        entry["thread_id"] = thread_id

    @staticmethod
    def _pop_phase_overlay(entry: dict[str, Any], owner: object) -> bool:
        overlays = entry.get("_phase_overlays")
        if not isinstance(overlays, list):
            return False
        retained = [overlay for overlay in overlays if overlay[0] is not owner]
        if len(retained) == len(overlays):
            return False
        if retained:
            entry["_phase_overlays"] = retained
            _, phase, thread_id = retained[-1]
        else:
            phase, thread_id = entry.pop(
                "_phase_base", (entry.get("phase"), entry["thread_id"])
            )
            entry.pop("_phase_overlays", None)
        entry["phase"] = phase
        entry["thread_id"] = thread_id
        return True

    def _exit_phase(
        self,
        context_token: contextvars.Token[_DiagnosticContext],
        changed: list[
            tuple[Optional[dict[int, dict[str, Any]]], int, dict[str, Any], object]
        ],
    ) -> None:
        try:
            with self._lock:
                restored = False
                for registry, identifier, captured_entry, owner in changed:
                    if registry is None:
                        if self._write_holder is captured_entry:
                            restored = (
                                self._pop_phase_overlay(captured_entry, owner)
                                or restored
                            )
                        continue
                    entry = registry.get(identifier)
                    if entry is captured_entry:
                        restored = self._pop_phase_overlay(entry, owner) or restored
                if restored:
                    self._changed_locked()
        finally:
            _diagnostic_context.reset(context_token)

    def _enter_sql(
        self, mode: str, sql: str | SqlMetadata, operation: str
    ) -> Optional[int]:
        metadata = normalize_sql_metadata(sql)
        entry = self._active_entry(
            mode=_bounded_text(mode, 24),
            operation=_bounded_text(operation, 160),
            verb=metadata.verb[:16],
            table=metadata.table[:128],
            fingerprint=metadata.fingerprint,
            **_context_fields(_diagnostic_context.get()),
        )
        with self._lock:
            return self._bounded_add_locked(self._active_sql, entry)

    def _exit_sql(self, token: Optional[int]) -> None:
        if token is None:
            return
        with self._lock:
            if self._active_sql.pop(token, None) is not None:
                self._changed_locked()

    def _enter_job(
        self, name: str
    ) -> tuple[Optional[int], contextvars.Token[_DiagnosticContext]]:
        previous = _diagnostic_context.get()
        job_id = f"job-{next(self._ids)}"
        context = _DiagnosticContext(
            request_id=previous.request_id,
            job_id=job_id,
            notebook_id=previous.notebook_id,
            phase=previous.phase,
        )
        context_token = _diagnostic_context.set(context)
        entry = self._active_entry(
            name=_bounded_text(name, 160),
            **_context_fields(context),
        )
        with self._lock:
            token = self._bounded_add_locked(self._active_jobs, entry)
        return token, context_token

    def _exit_job(
        self,
        token: Optional[int],
        context_token: contextvars.Token[_DiagnosticContext],
        status: str,
    ) -> None:
        try:
            if token is None:
                return
            with self._lock:
                entry = self._active_jobs.pop(token, None)
                if entry is None:
                    return
                finished = dict(entry)
                finished["status"] = status
                finished["completed_at"] = _utc_now()
                finished["duration_ms"] = round(
                    (time.monotonic() - finished.pop("_started_monotonic")) * 1_000,
                    3,
                )
                self._recent_jobs.append(finished)
                self._changed_locked()
        finally:
            _diagnostic_context.reset(context_token)

    def begin_write_wait(self, operation: str) -> Optional[int]:
        thread_id = threading.get_ident()
        with self._lock:
            if (
                self._write_holder is not None
                and self._write_holder["thread_id"] == thread_id
            ):
                return None
            entry = self._active_entry(
                operation=_bounded_text(operation, 160),
                **_context_fields(_diagnostic_context.get()),
            )
            return self._bounded_add_locked(self._write_waiters, entry)

    def write_wait_cancelled(self, waiter: object) -> None:
        if not isinstance(waiter, int):
            return
        with self._lock:
            if self._write_waiters.pop(waiter, None) is not None:
                self._changed_locked()

    def write_acquired(self, waiter: object, operation: str) -> None:
        thread_id = threading.get_ident()
        with self._lock:
            if (
                self._write_holder is not None
                and self._write_holder["thread_id"] == thread_id
            ):
                self._write_holder["depth"] += 1
                self._changed_locked()
                return
            if isinstance(waiter, int):
                self._write_waiters.pop(waiter, None)
            self._write_holder = self._active_entry(
                operation=_bounded_text(operation, 160),
                depth=1,
                **_context_fields(_diagnostic_context.get()),
            )
            self._changed_locked()

    def write_released(self) -> None:
        thread_id = threading.get_ident()
        with self._lock:
            if self._write_holder is None or self._write_holder["thread_id"] != thread_id:
                return
            self._write_holder["depth"] -= 1
            if self._write_holder["depth"] <= 0:
                self._write_holder = None
            self._changed_locked()

    @staticmethod
    def _render_entry(entry: dict[str, Any], now: float) -> dict[str, Any]:
        rendered = {
            key: value for key, value in entry.items() if not key.startswith("_")
        }
        rendered["duration_ms"] = round(
            max(0.0, now - entry["_started_monotonic"]) * 1_000, 3
        )
        return rendered

    def _provider_snapshot(self) -> tuple[dict[str, Any], dict[str, Any]]:
        failures = 0
        try:
            raw_readiness = self._readiness_provider()
        except Exception:
            raw_readiness = {}
            failures += 1
        try:
            raw_concurrency = self._concurrency_provider()
        except Exception:
            raw_concurrency = {}
            failures += 1
        if failures:
            self._snapshot_failed(failures)

        readiness: dict[str, Any] = {}
        if isinstance(raw_readiness, dict):
            if isinstance(raw_readiness.get("ready"), bool):
                readiness["ready"] = raw_readiness["ready"]
            if isinstance(raw_readiness.get("phase"), str):
                readiness["phase"] = raw_readiness["phase"][:80]
            for key in (
                "warmed_notebooks",
                "total_notebooks",
                "preloaded_indexes",
                "total_indexes",
            ):
                value = raw_readiness.get(key)
                if (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and 0 <= value <= _MAX_CONCURRENCY_VALUE
                ):
                    readiness[key] = value

        return readiness, _normalize_concurrency(raw_concurrency)

    def snapshot(self) -> dict[str, Any]:
        readiness, concurrency = self._provider_snapshot()
        now = time.monotonic()
        with self._lock:
            active_requests = [
                self._render_entry(entry, now)
                for entry in self._active_requests.values()
            ]
            active_sql = [
                self._render_entry(entry, now) for entry in self._active_sql.values()
            ]
            active_jobs = [
                self._render_entry(entry, now) for entry in self._active_jobs.values()
            ]
            waiters = [
                self._render_entry(entry, now)
                for entry in self._write_waiters.values()
            ]
            holder = (
                None
                if self._write_holder is None
                else self._render_entry(self._write_holder, now)
            )
            return {
                "schema_version": SCHEMA_VERSION,
                "pid": os.getpid(),
                "process_started_at": self._process_started_at,
                "heartbeat_at": _utc_now(),
                "last_state_change_at": self._last_state_change_at,
                "state_revision": self._state_revision,
                "snapshot_failures": self._snapshot_failures,
                "readiness": readiness,
                "concurrency": concurrency,
                "active_requests": active_requests,
                "active_sql": active_sql,
                "write_lock": {"holder": holder, "waiters": waiters},
                "active_jobs": active_jobs,
                "recent_jobs": [dict(entry) for entry in self._recent_jobs],
            }

    def _write_snapshot(self) -> None:
        temporary_name: Optional[str] = None
        temporary_descriptor: Optional[int] = None
        try:
            directory = self._validated_directory_descriptor()
            self._validate_optional_artifact(directory, "runtime.json")
            # A predictable temporary from an older process is never followed
            # or reused.  Reject unsafe leftovers so this remains fail-closed.
            self._validate_optional_artifact(directory, "runtime.json.tmp")
            snapshot = self.snapshot()
            payload = json.dumps(
                snapshot,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8", "strict")
            for _attempt in range(8):
                candidate = f".runtime.json.{secrets.token_hex(16)}.tmp"
                try:
                    temporary_descriptor = self._open_private_artifact(
                        directory,
                        candidate,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        created=True,
                    )
                except FileExistsError:
                    continue
                temporary_name = candidate
                break
            if temporary_descriptor is None or temporary_name is None:
                raise _UnsafeDiagnosticsArtifact()
            view = memoryview(payload)
            while view:
                written = os.write(temporary_descriptor, view)
                if written <= 0:
                    raise OSError(errno.EIO, "snapshot write failed")
                view = view[written:]
            os.fsync(temporary_descriptor)
            self._validate_private_file(os.fstat(temporary_descriptor))
            self._validated_directory_descriptor()
            self._validate_optional_artifact(directory, "runtime.json")
            os.replace(
                temporary_name,
                "runtime.json",
                src_dir_fd=directory,
                dst_dir_fd=directory,
            )
            temporary_name = None
            self._validate_optional_artifact(directory, "runtime.json")
        except BaseException:
            self._snapshot_failed()
        finally:
            if temporary_descriptor is not None:
                try:
                    os.close(temporary_descriptor)
                except OSError:
                    pass
            if temporary_name is not None and self._directory_fd is not None:
                try:
                    os.unlink(temporary_name, dir_fd=self._directory_fd)
                except OSError:
                    pass

    def _writer(self) -> None:
        writer = threading.current_thread()
        try:
            next_write = time.monotonic()
            while not self._stop.is_set():
                remaining = max(0.0, next_write - time.monotonic())
                self._wake.wait(remaining)
                self._wake.clear()
                if self._stop.is_set():
                    break
                if time.monotonic() < next_write:
                    continue
                self._write_snapshot()
                next_write = time.monotonic() + self._interval_seconds
        finally:
            self._begin_writer_exit(writer)

    def _begin_writer_exit(self, writer: threading.Thread) -> None:
        with self._lock:
            if self._thread is not writer:
                return
            self._lifecycle_state = "finalizing"
        self._release_signal()
        self._close_dump_handle()
        self._close_directory_descriptor()
        reaper = threading.Thread(
            target=self._reap_writer,
            args=(writer,),
            name="diagnostics-finalizer",
            daemon=True,
        )
        try:
            reaper.start()
        except Exception:
            self._snapshot_failed()
            self._mark_writer_stopped(writer)

    def _reap_writer(self, writer: threading.Thread) -> None:
        writer.join()
        self._mark_writer_stopped(writer)

    def _mark_writer_stopped(self, writer: Optional[threading.Thread]) -> None:
        with self._lock:
            if self._thread is not writer:
                return
            self._thread = None
            self._lifecycle_state = "stopped"

    def _claim_signal(self) -> bool:
        global _signal_owner
        if not (
            self._enable_signal
            and os.name == "posix"
            and hasattr(signal, "SIGUSR1")
            and threading.current_thread() is threading.main_thread()
            and self._dump_handle is not None
        ):
            return False
        with _signal_owner_lock:
            if _signal_owner is not None:
                return False
            try:
                faulthandler.register(
                    signal.SIGUSR1,
                    file=self._dump_handle,
                    all_threads=True,
                    chain=False,
                )
            except Exception:
                return False
            _signal_owner = self
            return True

    def _release_signal(self) -> None:
        global _signal_owner
        with _signal_owner_lock:
            if _signal_owner is not self:
                self._owns_signal = False
                self.signal_capture_available = False
                return
            try:
                faulthandler.unregister(signal.SIGUSR1)
            except Exception:
                pass
            _signal_owner = None
            self._owns_signal = False
            self.signal_capture_available = False

    def _close_dump_handle(self) -> None:
        handle = self._dump_handle
        self._dump_handle = None
        if handle is None:
            return
        try:
            handle.flush()
            handle.close()
        except Exception:
            pass

    @staticmethod
    def _directory_key(metadata: os.stat_result) -> tuple[int, int]:
        return int(metadata.st_dev), int(metadata.st_ino)

    @staticmethod
    def _validate_private_directory(metadata: os.stat_result) -> None:
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != _PRIVATE_DIRECTORY_MODE
        ):
            raise _UnsafeDiagnosticsArtifact()

    @staticmethod
    def _validate_private_file(metadata: os.stat_result) -> None:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != _PRIVATE_FILE_MODE
        ):
            raise _UnsafeDiagnosticsArtifact()

    def _open_diagnostics_directory(self) -> int:
        try:
            self.diagnostics_dir.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=True)
        except FileExistsError:
            pass
        descriptor = os.open(
            self.diagnostics_dir,
            os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
        )
        try:
            metadata = os.fstat(descriptor)
            path_metadata = os.stat(self.diagnostics_dir, follow_symlinks=False)
            self._validate_private_directory(metadata)
            self._validate_private_directory(path_metadata)
            if self._directory_key(metadata) != self._directory_key(path_metadata):
                raise _UnsafeDiagnosticsArtifact()
        except BaseException:
            os.close(descriptor)
            raise
        self._directory_fd = descriptor
        self._directory_identity = self._directory_key(metadata)
        return descriptor

    def _validated_directory_descriptor(self) -> int:
        descriptor = self._directory_fd
        identity = self._directory_identity
        if descriptor is None or identity is None:
            raise _UnsafeDiagnosticsArtifact()
        metadata = os.fstat(descriptor)
        path_metadata = os.stat(self.diagnostics_dir, follow_symlinks=False)
        self._validate_private_directory(metadata)
        self._validate_private_directory(path_metadata)
        if (
            self._directory_key(metadata) != identity
            or self._directory_key(path_metadata) != identity
        ):
            raise _UnsafeDiagnosticsArtifact()
        return descriptor

    def _open_private_artifact(
        self,
        directory: int,
        name: str,
        flags: int,
        *,
        created: bool = False,
    ) -> int:
        descriptor = os.open(
            name,
            flags | _NOFOLLOW | _CLOEXEC | _NONBLOCK,
            _PRIVATE_FILE_MODE,
            dir_fd=directory,
        )
        try:
            if created:
                os.fchmod(descriptor, _PRIVATE_FILE_MODE)
            self._validate_private_file(os.fstat(descriptor))
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _open_or_create_private_artifact(
        self, directory: int, name: str, flags: int
    ) -> int:
        try:
            return self._open_private_artifact(
                directory,
                name,
                flags | os.O_CREAT | os.O_EXCL,
                created=True,
            )
        except FileExistsError:
            return self._open_private_artifact(directory, name, flags)

    def _validate_optional_artifact(self, directory: int, name: str) -> None:
        try:
            metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return
        self._validate_private_file(metadata)

    def _close_directory_descriptor(self) -> None:
        descriptor = self._directory_fd
        self._directory_fd = None
        self._directory_identity = None
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def start(self) -> None:
        with self._lock:
            if self._lifecycle_state not in {"new", "stopped"}:
                return
            self._lifecycle_state = "starting"
            self._stop.clear()
            self._wake.set()
            try:
                directory = self._open_diagnostics_directory()
            except BaseException:
                self._snapshot_failed_locked()
                self._lifecycle_state = "stopped"
                return
            dump_descriptor: Optional[int] = None
            try:
                dump_descriptor = self._open_or_create_private_artifact(
                    directory,
                    "thread-dumps.log",
                    os.O_WRONLY | os.O_APPEND,
                )
                os.ftruncate(dump_descriptor, 0)
                self._dump_handle = os.fdopen(
                    dump_descriptor, "a", encoding="utf-8", buffering=1
                )
                dump_descriptor = None
            except BaseException:
                self._snapshot_failed_locked()
                self._dump_handle = None
                if dump_descriptor is not None:
                    try:
                        os.close(dump_descriptor)
                    except OSError:
                        pass

            self.signal_capture_available = self._claim_signal()
            self._owns_signal = self.signal_capture_available
            thread = threading.Thread(
                target=self._writer,
                name="diagnostics-snapshot",
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except Exception:
                self._snapshot_failed_locked()
                self._thread = None
                self._lifecycle_state = "stopped"
                self._release_signal()
                self._close_dump_handle()
                self._close_directory_descriptor()
                return
            self._lifecycle_state = "running"

    def stop(self) -> None:
        with self._lock:
            if self._lifecycle_state in {"new", "stopped"}:
                return
            if self._lifecycle_state == "running":
                self._lifecycle_state = "stopping"
                self._stop.set()
                self._wake.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2 * self._interval_seconds)
        if thread is not None and thread.is_alive():
            return
        self._mark_writer_stopped(thread)


def current_runtime() -> Optional[DiagnosticsRuntime]:
    with _runtime_lock:
        return _installed_runtime


@contextmanager
def install_runtime(runtime: DiagnosticsRuntime) -> Iterator[DiagnosticsRuntime]:
    global _installed_runtime, _installed_runtime_depth
    with _runtime_lock:
        if _installed_runtime is None:
            _installed_runtime = runtime
            _installed_runtime_depth = 1
        elif _installed_runtime is not runtime:
            raise RuntimeError("cannot install a different diagnostics runtime")
        else:
            _installed_runtime_depth += 1
    try:
        yield runtime
    finally:
        with _runtime_lock:
            if _installed_runtime is runtime:
                _installed_runtime_depth -= 1
                if _installed_runtime_depth == 0:
                    _installed_runtime = None


@contextmanager
def activate_runtime(
    diagnostics_dir: Path,
    readiness_provider: Callable[[], dict[str, Any]],
    concurrency_provider: Callable[[], dict[str, Any]],
    *,
    interval_seconds: float = 2.0,
    enable_signal: bool = True,
) -> Iterator[DiagnosticsRuntime]:
    runtime = DiagnosticsRuntime(
        diagnostics_dir,
        readiness_provider,
        concurrency_provider,
        interval_seconds=interval_seconds,
        enable_signal=enable_signal,
    )
    with install_runtime(runtime):
        runtime.start()
        try:
            yield runtime
        finally:
            runtime.stop()


@contextmanager
def request_scope(request_id: str, method: str, path: str) -> Iterator[None]:
    runtime = current_runtime()
    entered = None
    if runtime is not None:
        try:
            entered = runtime._enter_request(request_id, method, path)
        except Exception:
            entered = None
    try:
        yield
    finally:
        if runtime is not None and entered is not None:
            try:
                runtime._exit_request(*entered)
            except Exception:
                pass


@contextmanager
def diagnostic_phase(phase: str) -> Iterator[None]:
    runtime = current_runtime()
    entered = None
    if runtime is not None:
        try:
            entered = runtime._enter_phase(phase)
        except Exception:
            entered = None
    try:
        yield
    finally:
        if runtime is not None and entered is not None:
            try:
                runtime._exit_phase(*entered)
            except Exception:
                pass


@contextmanager
def sql_scope(
    mode: str, sql: str | SqlMetadata, operation: str
) -> Iterator[None]:
    runtime = current_runtime()
    token = None
    if runtime is not None:
        try:
            token = runtime._enter_sql(mode, sql, operation)
        except Exception:
            token = None
    try:
        yield
    finally:
        if runtime is not None:
            try:
                runtime._exit_sql(token)
            except Exception:
                pass


@contextmanager
def job_scope(name: str) -> Iterator[None]:
    runtime = current_runtime()
    entered = None
    status = "done"
    if runtime is not None:
        try:
            entered = runtime._enter_job(name)
        except Exception:
            entered = None
    try:
        yield
    except BaseException:
        status = "error"
        raise
    finally:
        if runtime is not None and entered is not None:
            try:
                runtime._exit_job(*entered, status)
            except Exception:
                pass


def begin_write_wait(operation: str) -> object:
    runtime = current_runtime()
    if runtime is None:
        return None
    try:
        return runtime.begin_write_wait(operation)
    except Exception:
        return None


def write_wait_cancelled(waiter: object) -> None:
    runtime = current_runtime()
    if runtime is None:
        return
    try:
        runtime.write_wait_cancelled(waiter)
    except Exception:
        pass


def write_acquired(waiter: object, operation: str) -> None:
    runtime = current_runtime()
    if runtime is None:
        return
    try:
        runtime.write_acquired(waiter, operation)
    except Exception:
        pass


def write_released() -> None:
    runtime = current_runtime()
    if runtime is None:
        return
    try:
        runtime.write_released()
    except Exception:
        pass
