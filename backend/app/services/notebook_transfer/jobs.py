"""Web export and import jobs of single notebooks (``.snbk``): the registry behind
``POST /api/notebooks/{id}/export``, ``POST /api/notebook-imports`` and
``/api/notebook-transfers`` (``app.api.notebook_transfer_routes``).

The user's rulings (docs/superpowers/plans/2026-10-22-notebook-export-import.md
§7, §9.3, §9.4) and the review's defaults:

* **State** lives in this process -- one registry per composition root -- and in
  one JSON sidecar per job under ``<storage>/transfers/jobs/``; no table, no
  migration. The deployment runs a single API process. Server startup
  (``recover_on_start``) loads the sidecars: a job that was ``queued`` or
  ``running`` is ``failed`` (``interrupted``) and its files go -- an import that
  had already published says so and keeps its notebook id; a finished job stays
  until it expires.
* **Bounds per user.** At most ``MAX_ACTIVE_JOBS_PER_USER`` queued or running
  jobs (``TransferBusy``); asking again to export a notebook whose export by the
  same user is still active returns that job; a finished export supersedes (and
  deletes) the older finished exports of the same notebook by the same user. An
  upload is held to ``WEB_IMPORT_MAX_INFLATION`` times the web limit once
  inflated, read off the zip directory before anything is decompressed.
* **Files.** An export is written by ``export.export_notebook`` to
  ``transfers/exports/<job>.snbk`` (its own ``<job>.snbk.<random>.part`` until it
  is complete, then published under the final name). An upload streams into
  ``transfers/imports/<job>.upload.part`` (the route), becomes ``<job>.snbk`` when
  the job is created, and is deleted when the import ends, whatever its outcome.
* **Expiry.** A finished job -- its record, sidecar and export file -- is kept
  ``NOTEBOOK_TRANSFER_TTL_SECONDS``. Reads (a job, the list, the bell, a ticket)
  drop expired jobs in memory only; ``sweep`` -- at startup, when a job is
  created and on the expiry timer -- also removes, under ``transfers/``, every
  file no job owns that has not been written to for that long (a part file of a
  killed export, an upload whose request died); startup removes every file no
  job owns at once (nothing can still be writing one). One expiry timer is armed
  at a time, for the next job to expire; each arming has its own generation and
  only the current one may fire and arm the next; ``close`` cancels it.
* **Work** runs in the heavy maintenance pool (``background_jobs``: job names
  ``notebook-export-<job>`` / ``notebook-import-<job>``). An import runs
  ``NotebookImportService.import_notebook`` (Knowhow tables are projected again
  inside it) and records the new notebook id on the sidecar the moment it is
  published (the ``project`` progress event); then it hands the re-embedding legs
  the report names to ``NotebookReembedService.submit`` -- the same single flight
  as ``backfill-vectors`` -- and only once that run has succeeded asks for the
  scale index (``maybe_auto_index``); a run that did not succeed builds none, the
  CLI's order (``scripts/notebook_archive.py``). Waiting for the run takes no pool
  slot: a per-job thread (``notebook-transfer-watch-<job>``) waits on it, at most
  ``REEMBED_WAIT_SECONDS``. Every run ends its job in a terminal state, whatever
  it raises.
* **Cancellation** (``delete`` of an active job) is cooperative: an export stops
  at its next checkpoint (every page of rows, every block of a file) and removes
  its part file; an import stops at its next progress event while it has not
  published (the import compensates: nothing of it remains). After publish the
  notebook exists and belongs to the importer: the record goes, the re-embedding
  and the index request still run, and ``delete`` returns the notebook's id.
* **Downloads** go through one-time tickets (§9.4): 32 random bytes, of which
  only the sha256 is kept, in memory, bound to ``(job, user)``, valid
  ``TICKET_TTL_SECONDS``. Redeeming one removes it in the same step under the
  registry lock (the compare-and-swap: of two concurrent redemptions exactly one
  finds it); it is then checked against the job (still there, the user's,
  a finished export, not expired) and the user (still active). Every refusal
  looks the same to the caller (``None``). The export counts as downloaded once
  the stream has been sent whole (``mark_downloaded``).

Logs carry job and notebook ids and exception class names only. A job's
``error`` is a code plus a complete Chinese sentence; its ``report`` holds counts
and ids, never a path (the export report's own ``path`` is left out).
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from app.core.config import Settings
from app.services import background_jobs
from app.services.notebook_reembed import NotebookReembedService, ReembedStatus
from app.services.notebook_transfer.export import (
    ExportReport,
    NotebookNotFoundError,
    export_notebook,
)
from app.services.notebook_transfer.import_ import (
    ImportProgress,
    ImportReport,
    NotebookImportError,
)
from app.services.pending_bus import pending_bus

_log = logging.getLogger("silicon_notebook.notebook_transfer")

EXPORT = "export"
IMPORT = "import"
QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
ACTIVE_STATES = frozenset({QUEUED, RUNNING})
TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, CANCELLED})
_KINDS = frozenset({EXPORT, IMPORT})

EXPORT_JOB_PREFIX = "notebook-export-"
IMPORT_JOB_PREFIX = "notebook-import-"
WATCH_JOB_PREFIX = "notebook-transfer-watch-"
"""``background_jobs`` names: the first two run in the heavy pool, the watcher
(which only waits) on its own thread."""

MAX_ACTIVE_JOBS_PER_USER = 2
"""Queued or running export/import jobs one user may have at once."""
WEB_IMPORT_MAX_INFLATION = 10
"""An upload may inflate to at most this many times ``NOTEBOOK_TRANSFER_WEB_MAX_MB``
(its zip directory's total, checked before anything is decompressed): an archive
this build writes compresses JSONL rows and text a few times over and stores
binary files as they are."""
DELETE_SETTLE_SECONDS = 30
"""How long DELETE of a running import waits for the run to settle -- to stop
before publish, or to publish -- before it answers ``pending``."""
REEMBED_WAIT_SECONDS = 24 * 3600
"""How long an import's watcher waits for its re-embedding run before it records
the run as still going and finishes the job."""
TICKET_TTL_SECONDS = 60
"""How long a download ticket can be redeemed (§9.4)."""
TICKET_BYTES = 32
DOWNLOAD_PATH = "/api/notebook-transfers/download"
#: Longest ticket string a redemption looks up (``token_urlsafe(32)`` is 43).
_MAX_TICKET_CHARS = 128

_JOB_ID = re.compile(r"^xfer-[0-9a-f]{32}$")
_ARCHIVE_SUFFIX = ".snbk"
_UPLOAD_PART_SUFFIX = ".upload.part"
_SIDECAR_SUFFIX = ".json"
#: Import stages at which a cancellation still stops the run: up to and including
#: ``publish`` the import compensates and nothing of it remains; after it the
#: notebook exists and nothing may undo it.
_CANCELLABLE_IMPORT_STAGES = frozenset({"preflight", "files", "rows", "validate", "publish"})
#: Import phases after publish: the new notebook exists.
_PUBLISHED_IMPORT_PHASES = frozenset({"project", "reembed", "index", "done"})
#: Fields a sidecar carries (everything but the in-memory cancellation state).
_PERSISTED = (
    "id", "kind", "user_id", "state", "phase", "created_at", "updated_at",
    "finished_at", "notebook_id", "notebook_name", "requested_name",
    "archive_bytes", "progress", "report", "reembed", "index", "error", "downloaded",
)

_MESSAGES = {
    "cancelled": "已取消。",
    "notebook_missing": "笔记本不存在或正在删除，无法导出。",
    "export_failed": "导出没有完成，请稍后重试。",
    "import_failed": "导入没有完成，请稍后重试。",
    "import_followup_failed": "笔记本已经导入，但导入后的后台处理没有完成；可在笔记本里重新补全检索数据。",
    "not_started": "暂时无法开始，请稍后重试。",
    "export_interrupted": "服务重启，这次导出没有完成，请重新导出。",
    "import_interrupted": "服务重启，这次导入没有完成，请重新导入。",
    "import_followup_interrupted": (
        "服务重启时笔记本已经导入，但导入后的后台处理没有完成；请不要重新导入，"
        "可在笔记本里重新补全检索数据。"
    ),
}


def too_large_message(limit_mb: int) -> str:
    return f"导出文件超过网页导出上限（{limit_mb} MB），请联系管理员用命令行导出这本笔记本。"


class TransferJobNotFound(KeyError):
    """No job with that id belongs to the caller (a job of another user is the
    same answer)."""


class TransferNotReady(RuntimeError):
    """A download ticket was asked for a job that is not a finished export."""


class TransferBusy(RuntimeError):
    """The user already has ``MAX_ACTIVE_JOBS_PER_USER`` active jobs."""


class _Stopped(Exception):
    """Raised inside a run whose job was cancelled."""


class _TooLarge(Exception):
    """Raised inside an export whose archive grew past the web limit."""


@dataclasses.dataclass(frozen=True)
class TransferDownload:
    """An opened export (the caller streams ``fd`` and closes it)."""

    fd: int
    size: int
    job_id: str
    notebook_name: str
    notebook_id: str


@dataclasses.dataclass(frozen=True)
class _Ticket:
    job_id: str
    user_id: str
    expires_at: float


@dataclasses.dataclass
class _Job:
    id: str
    kind: str
    user_id: str
    state: str
    phase: str
    created_at: float
    updated_at: float
    finished_at: "float | None" = None
    notebook_id: str = ""
    notebook_name: str = ""
    #: An import's name for the new notebook (``""``: the archive's).
    requested_name: str = ""
    archive_bytes: int = 0
    progress: dict = dataclasses.field(default_factory=dict)
    report: "dict | None" = None
    reembed: "dict | None" = None
    index: "dict | None" = None
    error: "dict | None" = None
    downloaded: bool = False
    # In memory only.
    cancel: threading.Event = dataclasses.field(default_factory=threading.Event, repr=False)
    #: Set once the run has decided whether its notebook stays: it stopped before
    #: publish, it published, or it ended.
    settled: threading.Event = dataclasses.field(default_factory=threading.Event, repr=False)
    deleted: bool = False

    @property
    def published(self) -> bool:
        """An import past publish in this process (its notebook exists). The
        notebook id alone does not say so: it is recorded from the sentinel on."""
        return self.kind == IMPORT and self.phase in _PUBLISHED_IMPORT_PHASES


def _new_job_id() -> str:
    return f"xfer-{secrets.token_hex(16)}"


def _iso(instant: "float | None") -> "str | None":
    if instant is None:
        return None
    return datetime.fromtimestamp(instant, timezone.utc).replace(microsecond=0).isoformat()


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _error(code: str, message: "str | None" = None) -> dict:
    return {"code": code, "message": message or _MESSAGES[code]}


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        _log.warning("notebook transfer: removing a file failed (%s)", type(exc).__name__)


def _daemon_timer(delay: float, action: Callable[[], Any]) -> threading.Timer:
    timer = threading.Timer(delay, action)
    timer.daemon = True
    timer.start()
    return timer


def _export_report(report: ExportReport) -> dict:
    """The export's receipt without its host path or part-file names."""
    return {
        "rows": dict(report.rows),
        "files": report.files,
        "file_bytes": report.file_bytes,
        "archive_bytes": report.archive_bytes,
        "vectors_included": report.vectors_included,
        "skipped_links": report.skipped_links,
        "missing_files": report.missing_files,
        "missing_source_files": report.missing_source_files,
        "normalized": dict(report.normalized),
    }


def _import_report(report: ImportReport) -> dict:
    result = report.to_dict()
    result.pop("dry_run", None)
    result.pop("interrupted", None)
    return result


def _reembed_summary(status: "ReembedStatus | None", legs: Iterable[str]) -> dict:
    if status is None:
        return {"state": "unknown", "legs": list(legs)}
    return {
        "run_id": status.run_id,
        "state": status.state,
        "legs": list(status.legs),
        "progress": {leg: dataclasses.asdict(p) for leg, p in status.progress.items()},
        "error": status.error,
    }


class NotebookTransferJobs:
    """The registry (module docstring). Collaborators are read at call time:
    ``storage_dir`` the storage root, ``export_rows`` the store's streaming copy
    read (and its ``exportable_file_bytes``), ``embeddings`` this deployment's
    embedding identity per workload, ``object_schemas`` the deployment-wide object
    types, ``importer`` the ``NotebookImportService``. ``submit`` runs work in the
    background (``background_jobs.submit``), ``notify(user_id)`` refreshes that
    user's pending-actions bell, ``timer(delay, action)`` arms the expiry timer
    and returns something with ``cancel()``; ``_clock`` is wall-clock seconds
    (tests replace it)."""

    def __init__(
        self,
        *,
        settings: Settings,
        storage_dir: Callable[[], Path],
        export_rows: Any,
        embeddings: Callable[[], Mapping[str, Any]],
        object_schemas: Callable[[], Iterable[Any]],
        importer: Callable[[], Any],
        submit: Callable[..., Any] = background_jobs.submit,
        notify: "Callable[[str], None] | None" = None,
        timer: Callable[[float, Callable[[], Any]], Any] = _daemon_timer,
    ) -> None:
        self._settings = settings
        self._storage_dir = storage_dir
        self._export_rows = export_rows
        self._embeddings = embeddings
        self._object_schemas = object_schemas
        self._importer = importer
        self._submit = submit
        self._notify_user = notify or pending_bus.mark_dirty
        self._timer = timer
        self._clock: Callable[[], float] = time.time
        self._lock = threading.RLock()
        self._jobs: dict[str, _Job] = {}
        self._tickets: dict[str, _Ticket] = {}
        #: Upload slots taken before the body is read: job id -> user id.
        self._reserved: dict[str, str] = {}
        #: Deleted jobs whose run has not ended yet: still holding their slot.
        self._draining: dict[str, _Job] = {}
        #: The armed expiry timer: when it is due, its generation, its handle.
        self._sweep_due: "float | None" = None
        self._sweep_generation = 0
        self._sweep_handle: Any = None
        self._closed = False

    # ------------------------------------------------------------- layout
    def _directory(self, name: str) -> Path:
        directory = Path(self._storage_dir()) / "transfers" / name
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _sidecar(self, job_id: str) -> Path:
        return self._directory("jobs") / f"{job_id}{_SIDECAR_SUFFIX}"

    def _export_path(self, job_id: str) -> Path:
        return self._directory("exports") / f"{job_id}{_ARCHIVE_SUFFIX}"

    def _upload_path(self, job_id: str) -> Path:
        return self._directory("imports") / f"{job_id}{_ARCHIVE_SUFFIX}"

    @property
    def _ttl(self) -> float:
        return float(self._settings.notebook_transfer_ttl_seconds)

    # ------------------------------------------------------------ bounds
    def export_source_bytes(self, notebook_id: str) -> int:
        """The bytes of the source files an export of the notebook would carry
        (Memory left out): the web export's pre-check against the web limit."""
        return int(self._export_rows.exportable_file_bytes(notebook_id))

    def import_inflation_bound(self) -> int:
        """The most an uploaded archive may inflate to (module docstring)."""
        return WEB_IMPORT_MAX_INFLATION * int(self._settings.notebook_transfer_web_max_bytes)

    def _check_capacity(self, user_id: str) -> None:
        """Under the lock: the user's slots are their queued or running jobs,
        their deleted jobs whose run has not ended, and their uploads still
        being received."""
        active = sum(
            1 for job in self._jobs.values()
            if job.user_id == user_id and job.state in ACTIVE_STATES
        )
        active += sum(1 for job in self._draining.values() if job.user_id == user_id)
        active += sum(1 for owner in self._reserved.values() if owner == user_id)
        if active >= MAX_ACTIVE_JOBS_PER_USER:
            raise TransferBusy(user_id)

    # ---------------------------------------------------------- creation
    def start_export(self, *, notebook_id: str, notebook_name: str, user_id: str) -> dict:
        """Queue an export of ``notebook_id`` for ``user_id`` (authorised by the
        caller) and return the job -- or the user's export of it that is still
        active. Raises ``TransferBusy``."""
        self.sweep()
        with self._lock:
            for existing in self._jobs.values():
                if (
                    existing.kind == EXPORT and existing.user_id == user_id
                    and existing.notebook_id == notebook_id
                    and existing.state in ACTIVE_STATES
                ):
                    return self._view(existing)
            self._check_capacity(user_id)
            now = self._clock()
            job = _Job(
                id=_new_job_id(), kind=EXPORT, user_id=user_id, state=QUEUED, phase=QUEUED,
                created_at=now, updated_at=now, notebook_id=notebook_id,
                notebook_name=notebook_name,
            )
            self._register(job)
        return self._launch(job, f"{EXPORT_JOB_PREFIX}{job.id}", self._run_export, job)

    def reserve_import(self, user_id: str) -> tuple[str, Path]:
        """Take one of the user's slots for an upload, before its body is read
        (``TransferBusy`` when none is free): a new job id and the part file the
        upload streams into (created by the caller, ``O_EXCL``). The slot is the
        job's once ``start_import`` takes it; the caller gives it back with
        ``release_upload`` on any other outcome."""
        job_id = _new_job_id()
        with self._lock:
            self._check_capacity(user_id)
            self._reserved[job_id] = user_id
        return job_id, self._directory("imports") / f"{job_id}{_UPLOAD_PART_SUFFIX}"

    def release_upload(self, job_id: str) -> None:
        """Give back the slot of an upload that did not become a job (idempotent)."""
        with self._lock:
            self._reserved.pop(job_id, None)

    def start_import(
        self, repo: Any, *, job_id: str, upload: Path, user_id: str, name: str
    ) -> dict:
        """Queue the import of the fully received ``upload`` (``reserve_import``'s
        part file) for ``user_id``, in the slot its reservation took; ``repo``
        (the repository facade) runs the re-embedding and the index request after
        publish."""
        self.sweep()
        if not _JOB_ID.fullmatch(job_id) or upload.name != f"{job_id}{_UPLOAD_PART_SUFFIX}":
            raise ValueError("not an upload of this registry")
        with self._lock:
            if self._reserved.get(job_id) != user_id:
                raise ValueError("no reserved slot for this upload")
            os.replace(upload, self._upload_path(job_id))
            del self._reserved[job_id]
            now = self._clock()
            job = _Job(
                id=job_id, kind=IMPORT, user_id=user_id, state=QUEUED, phase=QUEUED,
                created_at=now, updated_at=now, requested_name=name,
            )
            self._register(job)
        return self._launch(job, f"{IMPORT_JOB_PREFIX}{job.id}", self._run_import, repo, job)

    def _register(self, job: _Job) -> None:
        """Under the lock: the job enters the registry and gets its sidecar."""
        self._jobs[job.id] = job
        self._persist(job)

    def _launch(self, job: _Job, name: str, run: Callable[..., None], *args: Any) -> dict:
        try:
            self._submit(run, *args, name=name)
        except Exception as exc:  # noqa: BLE001 -- the job records it
            _log.error("notebook transfer: %s could not be submitted (%s)", job.id, type(exc).__name__)
            self._finish(job, FAILED, error=_error("not_started"))
        self._notify(job.user_id)
        with self._lock:
            return self._view(job)

    # -------------------------------------------------------------- reads
    def list_jobs(self, user_id: str) -> list[dict]:
        """``user_id``'s jobs, newest first."""
        self._expire()
        with self._lock:
            return [self._view(job) for job in self._jobs_of(user_id)]

    def job(self, user_id: str, job_id: str) -> dict:
        self._expire()
        with self._lock:
            return self._view(self._owned(user_id, job_id))

    def pending_items(self, user_id: str) -> list[dict]:
        """The pending-actions bell's entries of ``user_id``: every active job, a
        finished export not downloaded yet, and a failed job (until it expires)."""
        self._expire()
        with self._lock:
            items = []
            for job in self._jobs_of(user_id):
                shown = job.state in ACTIVE_STATES or job.state == FAILED or (
                    job.kind == EXPORT and job.state == SUCCEEDED and not job.downloaded
                )
                if shown:
                    items.append({
                        "type": "notebook_transfer",
                        "kind": job.kind,
                        "state": job.state,
                        "phase": job.phase,
                        "job_id": job.id,
                        "notebook_id": job.notebook_id,
                        "notebook_name": job.notebook_name,
                        "progress": dict(job.progress),
                    })
            return items

    def _jobs_of(self, user_id: str) -> list[_Job]:
        return sorted(
            (job for job in self._jobs.values() if job.user_id == user_id),
            key=lambda job: (job.created_at, job.id), reverse=True,
        )

    def _owned(self, user_id: str, job_id: str) -> _Job:
        job = self._jobs.get(job_id)
        if job is None or job.user_id != user_id:
            raise TransferJobNotFound(job_id)
        return job

    def _view(self, job: _Job) -> dict:
        expires = None if job.finished_at is None else job.finished_at + self._ttl
        return copy.deepcopy({
            "id": job.id,
            "kind": job.kind,
            "state": job.state,
            "phase": job.phase,
            "notebook_id": job.notebook_id,
            "notebook_name": job.notebook_name,
            "created_at": _iso(job.created_at),
            "updated_at": _iso(job.updated_at),
            "finished_at": _iso(job.finished_at),
            "expires_at": _iso(expires),
            "progress": job.progress,
            "archive_bytes": job.archive_bytes,
            "downloadable": job.kind == EXPORT and job.state == SUCCEEDED,
            "downloaded": job.downloaded,
            "report": job.report,
            "reembed": job.reembed,
            "index": job.index,
            "error": job.error,
        })

    # ------------------------------------------------------------ delete
    def delete(self, user_id: str, job_id: str) -> dict:
        """Cancel ``user_id``'s active job or delete a finished one (module
        docstring); its record, sidecar, tickets and files go. Returns
        ``{"outcome", "kept_notebook_id"}``: ``deleted`` (a finished job),
        ``cancelled`` (stopped; nothing of it stays), ``kept`` (an import that has
        created its notebook -- it stays, ``kept_notebook_id`` names it) or
        ``pending`` (a running import that did not settle within
        ``DELETE_SETTLE_SECONDS``: it is told to stop and stays readable until it
        has, ending ``cancelled`` -- or, had it published meanwhile, going on
        with its notebook).

        A running import is waited for until it has settled, because only then
        is it known whether its notebook stays (a cancellation that arrives while
        it publishes cannot stop it). A deleted job whose run is still going
        keeps its slot until the run ends."""
        with self._lock:
            job = self._owned(user_id, job_id)
            job.cancel.set()
            wait = job.kind == IMPORT and job.state == RUNNING and not job.settled.is_set()
        if wait:
            job.settled.wait(DELETE_SETTLE_SECONDS)
        with self._lock:
            if job.deleted or self._jobs.get(job.id) is not job:
                raise TransferJobNotFound(job_id)  # deleted concurrently
            undecided = job.state == RUNNING and job.kind == IMPORT and not (
                job.settled.is_set() or job.published
            )
            if undecided:
                return {"outcome": "pending", "kept_notebook_id": None}
            kept = job.notebook_id if job.published and job.notebook_id else None
            running = job.state == RUNNING
            if job.state in TERMINAL_STATES:
                outcome = "deleted"
            else:
                outcome = "kept" if kept else "cancelled"
            job.deleted = True
            self._forget(job)
            if running:
                self._draining[job.id] = job
        if not running:
            # A running job removes its own files when it stops.
            self._remove_files(job)
        self._notify(user_id)
        return {"outcome": outcome, "kept_notebook_id": kept}

    def _forget(self, job: _Job) -> None:
        """Drop the job from the registry, its tickets and its sidecar (under the lock)."""
        self._jobs.pop(job.id, None)
        for digest, ticket in list(self._tickets.items()):
            if ticket.job_id == job.id:
                del self._tickets[digest]
        _unlink(self._sidecar(job.id))

    def _remove_files(self, job: _Job) -> None:
        if job.kind == EXPORT:
            _unlink(self._export_path(job.id))
        else:
            _unlink(self._upload_path(job.id))

    # ------------------------------------------------------------ expiry
    def _expire(self) -> int:
        """Drop every expired job (its record, sidecar, tickets and own files) and
        every expired ticket -- no directory scan. Returns how many expired."""
        now = self._clock()
        with self._lock:
            expired = [
                job for job in self._jobs.values()
                if job.state in TERMINAL_STATES
                and job.finished_at is not None
                and job.finished_at + self._ttl <= now
            ]
            for job in expired:
                job.deleted = True
                self._forget(job)
            for digest, ticket in list(self._tickets.items()):
                if ticket.expires_at <= now:
                    del self._tickets[digest]
        for job in expired:
            self._remove_files(job)
        for user_id in {job.user_id for job in expired}:
            self._notify(user_id)
        return len(expired)

    def sweep(self) -> int:
        """``_expire`` plus the files under ``transfers/`` no job owns (module
        docstring). Returns how many jobs expired."""
        expired = self._expire()
        self._sweep_files(self._clock(), everything=False)
        return expired

    def _sweep_files(self, now: float, *, everything: bool) -> None:
        """Remove the files under ``transfers/`` that no job owns: all of them at
        startup (``everything``), otherwise those untouched for the TTL."""
        with self._lock:
            active = {job.id for job in self._jobs.values() if job.state in ACTIVE_STATES}
            exports = {job.id for job in self._jobs.values() if job.kind == EXPORT}
            known = set(self._jobs)
        candidates: list[tuple[Path, bool]] = []
        for path in self._directory("exports").iterdir():
            job_id = path.name.split(".", 1)[0]
            # A part file belongs to a running export; the archive to its job.
            owner = active if path.name.endswith(".part") else exports
            candidates.append((path, job_id in owner))
        for path in self._directory("imports").iterdir():
            candidates.append((path, path.name.split(".", 1)[0] in active))
        for path in self._directory("jobs").iterdir():
            candidates.append((
                path,
                path.name.endswith(_SIDECAR_SUFFIX) and path.name[: -len(_SIDECAR_SUFFIX)] in known,
            ))
        for path, owned in candidates:
            if owned:
                continue
            if not everything:
                try:
                    if path.lstat().st_mtime + self._ttl > now:
                        continue
                except FileNotFoundError:
                    continue
            _unlink(path)

    def _schedule_sweep(self, due: float) -> None:
        """Arm the expiry timer for ``due`` unless one is already armed no later
        (an earlier arming supersedes and cancels a later one)."""
        with self._lock:
            if self._closed or (self._sweep_due is not None and self._sweep_due <= due):
                return
            previous = self._sweep_handle
            self._sweep_generation += 1
            generation = self._sweep_generation
            self._sweep_due = due
            delay = max(0.0, due - self._clock()) + 1.0
            self._sweep_handle = self._timer(delay, lambda: self._timed_sweep(generation))
        if previous is not None:
            previous.cancel()

    def _timed_sweep(self, generation: int) -> None:
        """The armed timer firing: only the current generation sweeps and arms
        the next one (a superseded or cancelled one does nothing)."""
        with self._lock:
            if self._closed or generation != self._sweep_generation:
                return
            self._sweep_due = None
            self._sweep_handle = None
        try:
            self.sweep()
        except Exception as exc:  # noqa: BLE001 -- the next creation sweeps again
            _log.warning("notebook transfer: timed sweep failed (%s)", type(exc).__name__)
        self._arm_next()

    def _arm_next(self) -> None:
        with self._lock:
            pending = [
                job.finished_at + self._ttl for job in self._jobs.values()
                if job.finished_at is not None
            ]
        if pending:
            self._schedule_sweep(min(pending))

    def close(self) -> None:
        """Cancel the armed expiry timer (the repository is closing)."""
        with self._lock:
            self._closed = True
            handle, self._sweep_handle = self._sweep_handle, None
            self._sweep_due = None
            self._sweep_generation += 1
        if handle is not None:
            handle.cancel()

    def recover_on_start(self) -> int:
        """Server startup only (an offline tool cannot claim no live job owns a
        sidecar): load every sidecar, fail the jobs that were active
        (``interrupted``; an import that had published keeps its notebook id and
        says so), drop expired jobs and every file no job owns. Returns how many
        jobs were failed. The re-embedding of an interrupted import is not
        resubmitted here: its notebook is live and ``backfill-vectors`` (the
        notebook's own repair) fills what is missing, while starting model work
        for every interrupted import at boot would compete with the warm-up."""
        now = self._clock()
        failed = 0
        for path in sorted(self._directory("jobs").glob(f"*{_SIDECAR_SUFFIX}")):
            job = _load_sidecar(path)
            if job is None:
                _unlink(path)
                continue
            if job.state in ACTIVE_STATES:
                code = (
                    "export_interrupted" if job.kind == EXPORT
                    else "import_followup_interrupted" if self._was_published(job)
                    else "import_interrupted"
                )
                job.state, job.error = FAILED, _error(code)
                job.finished_at = job.updated_at = now
                failed += 1
            elif job.kind == EXPORT and job.state == SUCCEEDED and not self._export_path(
                job.id
            ).is_file():
                _unlink(path)  # its archive is gone: nothing left to download
                continue
            with self._lock:
                self._register(job)
        self.sweep()
        self._sweep_files(now, everything=True)
        self._arm_next()
        return failed

    def _was_published(self, job: _Job) -> bool:
        """Whether an interrupted import had published its notebook, decided by
        the database: the row exists and is no longer the hidden ``importing``
        sentinel (absent: compensated or reaped; ``deleting``: handed to the
        delete job, unless the job had got past publish). Without a recorded id
        nothing was written."""
        if not job.notebook_id:
            return False
        try:
            status = self._export_rows.notebook_lifecycle_status(job.notebook_id)
        except Exception as exc:  # noqa: BLE001 -- fall back to what the sidecar says
            _log.warning("notebook transfer: import status read failed (%s)", type(exc).__name__)
            return job.published
        if status is None or status == "importing":
            return False
        return status != "deleting" or job.published

    # ----------------------------------------------------------- tickets
    def issue_ticket(self, user_id: str, job_id: str) -> dict:
        """A one-time download ticket for ``user_id``'s finished export:
        ``{"url", "expires_at"}``. Raises ``TransferJobNotFound`` /
        ``TransferNotReady``."""
        self._expire()
        now = self._clock()
        with self._lock:
            job = self._owned(user_id, job_id)
            if job.kind != EXPORT or job.state != SUCCEEDED:
                raise TransferNotReady(job_id)
            token = secrets.token_urlsafe(TICKET_BYTES)
            self._tickets[_digest(token)] = _Ticket(
                job_id=job.id, user_id=user_id, expires_at=now + TICKET_TTL_SECONDS
            )
        return {
            "url": f"{DOWNLOAD_PATH}?ticket={token}",
            "expires_at": _iso(now + TICKET_TTL_SECONDS),
        }

    def redeem_ticket(
        self, token: str, *, user_active: Callable[[str], bool]
    ) -> "TransferDownload | None":
        """The export a ticket names, opened, or ``None`` -- for an unknown,
        replayed or expired ticket, a job that is gone, expired or not the
        ticket user's finished export, and a user who is no longer active alike.
        The ticket is spent by the attempt whatever its outcome."""
        if not isinstance(token, str) or not token or len(token) > _MAX_TICKET_CHARS:
            return None
        digest = _digest(token)
        now = self._clock()
        with self._lock:
            ticket = self._tickets.pop(digest, None)
        if ticket is None or ticket.expires_at <= now:
            return None
        with self._lock:
            job = self._jobs.get(ticket.job_id)
            if (
                job is None or job.deleted or job.user_id != ticket.user_id
                or job.kind != EXPORT or job.state != SUCCEEDED
                or job.finished_at is None or job.finished_at + self._ttl <= now
            ):
                return None
            path = self._export_path(job.id)
        try:
            if not user_active(ticket.user_id):
                return None
        except Exception as exc:  # noqa: BLE001 -- unknown means no
            _log.warning("notebook transfer: user check failed (%s)", type(exc).__name__)
            return None
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return None
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            return None
        return TransferDownload(
            fd=fd, size=info.st_size, job_id=job.id, notebook_name=job.notebook_name,
            notebook_id=job.notebook_id,
        )

    def mark_downloaded(self, job_id: str) -> None:
        """The export's stream was sent whole: it no longer waits in the bell."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.deleted:
                return
            job.downloaded = True
            job.updated_at = self._clock()
            self._persist(job)
        self._notify(job.user_id)

    # -------------------------------------------------------------- runs
    def _claim(self, job: _Job, phase: str) -> bool:
        with self._lock:
            if job.deleted or job.state != QUEUED:
                return False
            job.state, job.phase = RUNNING, phase
            job.updated_at = self._clock()
            self._persist(job)
        self._notify(job.user_id)
        return True

    def _set_phase(self, job: _Job, phase: str, **fields: Any) -> None:
        with self._lock:
            job.phase = phase
            for name, value in fields.items():
                setattr(job, name, value)
            job.updated_at = self._clock()
            if not job.deleted:
                self._persist(job)
        self._notify(job.user_id)

    def _finish(self, job: _Job, state: str, **fields: Any) -> None:
        with self._lock:
            if job.state in TERMINAL_STATES:
                return
            for name, value in fields.items():
                setattr(job, name, value)
            job.state = state
            job.finished_at = job.updated_at = self._clock()
            self._draining.pop(job.id, None)
            job.settled.set()
            dropped = job.deleted
            superseded = []
            if not dropped:
                self._persist(job)
                if job.kind == EXPORT and state == SUCCEEDED:
                    superseded = [
                        older for older in self._jobs.values()
                        if older is not job and older.kind == EXPORT
                        and older.user_id == job.user_id
                        and older.notebook_id == job.notebook_id
                        and older.state == SUCCEEDED
                    ]
                    for older in superseded:
                        older.deleted = True
                        self._forget(older)
        for older in superseded:
            self._remove_files(older)
        if dropped or job.kind == IMPORT or state != SUCCEEDED:
            self._remove_files(job)
        if not dropped:
            self._schedule_sweep(job.finished_at + self._ttl)
        self._notify(job.user_id)

    def _ensure_finished(self, job: _Job, code: str) -> None:
        """The outermost guard of every run: a job still active when its run
        ends (something raised past the run's own handling) is failed."""
        if job.state in TERMINAL_STATES:
            return
        if job.published:
            code = "import_followup_failed"
        self._finish(job, FAILED, error=_error(code))

    def _run_export(self, job: _Job) -> None:
        try:
            self._export(job)
        finally:
            self._ensure_finished(job, "export_failed")

    def _export(self, job: _Job) -> None:
        if not self._claim(job, "writing"):
            return
        limit = int(self._settings.notebook_transfer_web_max_bytes)
        out = self._export_path(job.id)

        def checkpoint(written: int) -> None:
            if job.cancel.is_set():
                raise _Stopped
            if written > limit:
                raise _TooLarge
            job.progress = {"bytes": int(written)}

        too_large = _error("too_large", too_large_message(self._settings.notebook_transfer_web_max_mb))
        try:
            report = export_notebook(
                self._export_rows, job.notebook_id, out,
                storage_dir=Path(self._storage_dir()), settings=self._settings,
                embeddings=self._embeddings(),
                global_object_schemas=self._object_schemas(),
                checkpoint=checkpoint,
            )
        except _Stopped:
            self._finish(job, CANCELLED, error=_error("cancelled"))
            return
        except _TooLarge:
            self._finish(job, FAILED, error=too_large)
            return
        except NotebookNotFoundError:
            self._finish(job, FAILED, error=_error("notebook_missing"))
            return
        except Exception as exc:  # noqa: BLE001 -- recorded on the job
            _log.error(
                "notebook transfer: export %s of %s failed (%s)",
                job.id, job.notebook_id, type(exc).__name__,
            )
            self._finish(job, FAILED, error=_error("export_failed"))
            return
        if report.archive_bytes > limit:
            # The manifest and the zip directory come after the last checkpoint.
            self._finish(job, FAILED, error=too_large)
            return
        self._finish(
            job, SUCCEEDED, phase="done", archive_bytes=report.archive_bytes,
            progress={"bytes": report.archive_bytes}, report=_export_report(report),
        )

    def _run_import(self, repo: Any, job: _Job) -> None:
        upload = self._upload_path(job.id)
        try:
            try:
                report = self._import(job, upload)
            finally:
                _unlink(upload)
            if report is not None:
                self._set_phase(
                    job, "reembed" if report.reembed_legs else "index",
                    notebook_id=report.notebook_id, notebook_name=report.name,
                    report=_import_report(report),
                )
                self._after_import(repo, job, report)
        finally:
            if not job.reembed or job.reembed.get("state") != "running":
                # The watcher finishes a job whose re-embedding it waits for.
                self._ensure_finished(job, "import_failed")

    def _import(self, job: _Job, upload: Path) -> "ImportReport | None":
        """The import itself; ``None`` when it did not publish (the job is finished)."""
        if not self._claim(job, "preflight"):
            return None
        written: dict[str, int] = {}

        def progress(event: ImportProgress) -> None:
            if job.cancel.is_set() and event.stage in _CANCELLABLE_IMPORT_STAGES:
                job.settled.set()  # decided: nothing of it stays
                raise _Stopped
            self._import_progress(job, event, written)

        try:
            return self._importer().import_notebook(
                upload, owner_id=job.user_id, name=job.requested_name or None,
                progress=progress, max_inflated_bytes=self.import_inflation_bound(),
            )
        except _Stopped:
            self._finish(job, CANCELLED, error=_error("cancelled"))
        except NotebookImportError as exc:
            # The importer's own refusal: a complete sentence that names what is
            # wrong with the file and never quotes its content or a path.
            self._finish(job, FAILED, error=_error("invalid_archive", f"文件无法导入：{exc}"))
        except Exception as exc:  # noqa: BLE001 -- recorded on the job
            _log.error("notebook transfer: import %s failed (%s)", job.id, type(exc).__name__)
            self._finish(job, FAILED, error=_error("import_failed"))
        return None

    def _import_progress(self, job: _Job, event: ImportProgress, written: dict) -> None:
        if event.stage == "done":
            return
        if event.stage == "rows":
            written[event.table] = event.rows
            job.progress = {"rows": sum(written.values()), "rows_total": event.rows_total}
        if event.notebook_id and event.notebook_id != job.notebook_id:
            # The sentinel is committed: its id is on the sidecar from here on, so a
            # restart can ask the database whether it was published.
            self._set_phase(job, event.stage, notebook_id=event.notebook_id)
        elif event.stage != job.phase:
            self._set_phase(job, event.stage)
        if job.published:
            job.settled.set()  # decided: the notebook stays

    def _after_import(self, repo: Any, job: _Job, report: ImportReport) -> None:
        """Re-embed what the import names, then ask for the scale index once that
        run succeeded (module docstring)."""
        legs = tuple(report.reembed_legs)
        if not legs:
            self._finish(job, SUCCEEDED, phase="done", index=self._request_index(repo, job))
            return
        try:
            submission = NotebookReembedService(repo).submit(report.notebook_id, legs=legs)
        except Exception as exc:  # noqa: BLE001 -- the import itself is done
            _log.error(
                "notebook transfer: re-embedding %s could not start (%s)",
                report.notebook_id, type(exc).__name__,
            )
            self._finish(
                job, SUCCEEDED, phase="done",
                reembed={"state": "failed", "legs": list(legs), "error": type(exc).__name__},
                index={"state": "skipped", "reason": "reembed_not_done"},
            )
            return
        self._set_phase(
            job, "reembed",
            reembed={"run_id": submission.run_id, "state": "running", "legs": list(legs)},
        )
        try:
            self._submit(
                self._watch_reembed, repo, job, submission.run_id, legs,
                name=f"{WATCH_JOB_PREFIX}{job.id}",
            )
        except Exception as exc:  # noqa: BLE001 -- the import itself is done
            _log.error("notebook transfer: %s watcher could not start (%s)", job.id, type(exc).__name__)
            self._finish(
                job, SUCCEEDED, phase="done",
                index={"state": "skipped", "reason": "reembed_not_done"},
            )

    def _watch_reembed(self, repo: Any, job: _Job, run_id: str, legs: tuple[str, ...]) -> None:
        try:
            status = NotebookReembedService.wait(run_id, REEMBED_WAIT_SECONDS)
            summary = _reembed_summary(status, legs)
            if status is not None and status.state == "succeeded":
                index = self._request_index(repo, job)
            elif status is not None and status.state == "running":
                index = {"state": "skipped", "reason": "reembed_still_running"}
            else:
                index = {"state": "skipped", "reason": "reembed_not_done"}
            self._finish(job, SUCCEEDED, phase="done", reembed=summary, index=index)
        finally:
            self._ensure_finished(job, "import_failed")

    def _request_index(self, repo: Any, job: _Job) -> dict:
        """``maybe_auto_index`` decides whether the notebook needs a scale index
        and builds one when its automatic conditions hold (it fails open)."""
        self._set_phase(job, "index")
        try:
            repo.maybe_auto_index(job.notebook_id)
        except Exception as exc:  # noqa: BLE001 -- the import itself is done
            _log.error(
                "notebook transfer: index request for %s failed (%s)",
                job.notebook_id, type(exc).__name__,
            )
            return {"state": "failed", "error": type(exc).__name__}
        return {"state": "requested"}

    # ----------------------------------------------------------- helpers
    def _persist(self, job: _Job) -> None:
        """Write the job's sidecar (under the lock). A write that fails is logged:
        the record in memory stays authoritative while this process lives."""
        path = self._sidecar(job.id)
        temporary = path.with_name(f"{path.name}.tmp")
        data = {name: getattr(job, name) for name in _PERSISTED}
        try:
            temporary.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), "utf-8")
            os.replace(temporary, path)
        except OSError as exc:
            _log.error("notebook transfer: writing %s failed (%s)", job.id, type(exc).__name__)

    def _notify(self, user_id: str) -> None:
        try:
            self._notify_user(user_id)
        except Exception as exc:  # noqa: BLE001 -- a bell refresh never fails a job
            _log.warning("notebook transfer: bell refresh failed (%s)", type(exc).__name__)


def _load_sidecar(path: Path) -> "_Job | None":
    """A sidecar's job, or ``None`` for a file that is not one this build wrote."""
    job_id = path.name[: -len(_SIDECAR_SUFFIX)]
    if not _JOB_ID.fullmatch(job_id):
        return None
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("id") != job_id:
        return None
    if data.get("kind") not in _KINDS or data.get("state") not in ACTIVE_STATES | TERMINAL_STATES:
        return None
    try:
        return _Job(
            id=job_id,
            kind=data["kind"],
            user_id=str(data["user_id"]),
            state=data["state"],
            phase=str(data.get("phase") or ""),
            created_at=float(data["created_at"]),
            updated_at=float(data["updated_at"]),
            finished_at=None if data.get("finished_at") is None else float(data["finished_at"]),
            notebook_id=str(data.get("notebook_id") or ""),
            notebook_name=str(data.get("notebook_name") or ""),
            requested_name=str(data.get("requested_name") or ""),
            archive_bytes=int(data.get("archive_bytes") or 0),
            progress=dict(data.get("progress") or {}),
            report=data.get("report") if isinstance(data.get("report"), dict) else None,
            reembed=data.get("reembed") if isinstance(data.get("reembed"), dict) else None,
            index=data.get("index") if isinstance(data.get("index"), dict) else None,
            error=data.get("error") if isinstance(data.get("error"), dict) else None,
            downloaded=bool(data.get("downloaded")),
        )
    except (KeyError, TypeError, ValueError):
        return None
