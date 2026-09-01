"""Batch 3·W1 PR-3: tombstone + the six-phase delete job's orchestration
(design doc §T-2/§T-3/§T-4).

**Phase B** (this module) implements the real batched cleanup:

- **Phase 3 (rows)**: drives ``services.notebook_delete_tables.PHASE_3_PLAN``
  — the ordered, declarative list of every direct-delete table (form-one/
  form-two, §1.5) and B-class parent-driven chain (§1.3) — through the
  generic and chain-specific primitives on ``NotebookDeleteJobStore``. Every
  one of the 65 closure tables + 6 closure-external tables is cleared
  EXCEPT the four archive-input tables (``ask_jobs``/``sources``/``reports``/
  ``source_paper_meta``) and ``answers`` (a fifth, undocumented archive-read
  dependency found during implementation — see ``notebook_delete_tables``'s
  module docstring), whose OWN rows stay live for phase 5 to archive and
  delete/cascade in its single transaction.
- **Phase 4 (files)**: deletes source files (paged from the ``notebook_
  delete_files`` side table phase 1 materialized), the pasted-image asset
  directory, and the three scale-artifact roots (``kg_index``/``kg_viz``/
  ``kg_index_partitions``) plus their ``.old``/``.tmp``/``.tmp-<token>``
  siblings (§T-3b).

**Code-review round (P1-B): the independent per-notebook claim now spans
phases 3-5, not just phase 4.** It is acquired once, right before phase 3
starts, held through phases 3/4/5, and released only after phase 5 commits
(or, in residual-cleanup mode, after that mode's own cleanup-only finish) —
never re-acquired per phase. ``verify_held()`` is re-checked before EVERY
destructive phase-3/4 batch and once more immediately before phase 5's
transaction (§4.3's "相位 3 每批前 verify_held，相位 4/5 各复验"). A claim
that cannot be taken at all (held elsewhere, or the lock backend's session
budget is exhausted, or an exception escapes the probe itself) parks the job
``'queued'`` (NOT ``'waiting'`` — see ``mark_queued``'s docstring for why
that status is reserved for phase 2's quiesce alone) for the sweep to retry
— it never forces through without holding the claim. On SQLite (whose
``try_scale_build_lock`` is an unconditionally-granted sentinel — there is
no cross-process lock to take), the SAME claim ALSO registers into
``ScaleArtifactRuntime``'s in-process ``building`` set (see
``_acquire_claim``) so an in-process scale build and a delete's phases 3-5
exclude each other the same way two in-process scale builds already do —
closing the gap a PostgreSQL-only claim otherwise leaves wide open in a
single-process SQLite deployment.

**Code-review round (P1-A): the sweep driver-A "job row present, notebooks
row absent" special case is now real, not aspirational.** ``run()`` decides
ONCE, right after claiming ownership, whether the notebook row still exists
(``notebook_exists``). If it does not — an out-of-band delete beat this job
to it (a legacy unbounded ``delete_notebook`` call, a ``sweep_stale_copies``
misfire, or a manual DBA delete) — every phase-3/4 liveness check switches
from "is the notebook still ``'deleting'``" to "is the notebook still
absent" (``_still_owned``'s ``residual`` flag), and phase 5's fence+archive
are skipped entirely in favor of ``_finish_residual`` — see that method's
own docstring for why re-archiving after the fact is never attempted.

**Code-review round (P2-a): owner/lease fencing.** Every successful
``mark_running`` (including a sweep-driven steal of a stale-but-still-
'running' row — see that method's docstring) mints a fresh ``lease_token``;
every write this ``run()`` invocation issues from then on carries it, so a
worker that has merely gone slow (not actually dead) writes nothing once a
second resubmission has taken over, even if it keeps executing past the
point it lost ownership. ``ownership_snapshot`` combines the job-row and
notebook-row liveness checks into one query (replacing the former two-point
``_still_deleting``).

**Code-review round (P1-E): failed jobs back off and stop, they do not
retry forever.** Sweep driver B (``list_notebooks_missing_job``) now carries
each candidate notebook's most recent failed attempt count and timestamp;
``sweep_once`` applies an exponential backoff window and a hard attempt
ceiling (``_MAX_DELETE_ATTEMPTS``) before recreating a job, and purges the
notebook's old failed job rows (and their orphaned ``notebook_delete_files``
side-table rows — a failed job's ``finish()`` never cleans those up) before
inserting the new one.

Six phases, resumed via the job row's ``phase`` column (§T-4's "幂等重排"):
``phase`` records the LAST phase that fully completed, so a resumed run
picks up at the NEXT phase after it — 'mark' (phase 0, done by ``request``)
→ 'paths' (phase 1 done) → 'quiesce' (phase 2 done) → 'rows' (phase 3 done)
→ 'files' (phase 4 done) → finalize (phase 5; the job row is deleted by
finalize/``_finish_residual`` itself, so there is no 'finalize' phase value
ever persisted). WHILE a phase is in progress its OWN cursor is persisted
with the job's ``phase`` column still reading the PREVIOUS (last
fully-completed) phase name — e.g. phase 3's per-unit progress is written
with ``phase='quiesce'`` still set, exactly mirroring how phase 1's per-page
progress is written with ``phase='mark'`` still set."""
from __future__ import annotations

import logging
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.repositories.filesystem.scale_artifact_store import (
    SCRATCH_INFIX,
    SCRATCH_SUFFIXES,
)
from app.repositories.ports import NotebookAlreadyDeletingError
from app.repositories.scale_build_lock import (
    SCALE_BUILD_LOCK_UNAVAILABLE,
    ScaleBuildLockAttempt,
)
from app.repositories.source_files import delete_source_file
from app.services import background_jobs
from app.services.notebook_catalog import _delete_notebook_asset_dir
from app.services.notebook_delete_tables import (
    PHASE_3_PLAN,
    CURSOR_KEYS,
    Chain,
    DirectTable,
)

_log = logging.getLogger("silicon_notebook.notebook_delete")

# §1.5's房规 batch size for the paths-materialization keyset page (phase 1)
# AND phase 3's row-cleanup batches (form-one page size / form-two ctid
# batch size) — the design gives ONE starting value (500) for both forms and
# registers 2000 as a future per-large-table tuning headroom, not a second
# required knob; a single module constant satisfies "批大小配置化" without
# inventing an unrequested per-table config surface.
_PATHS_PAGE_SIZE = 500
_ROWS_PAGE_SIZE = 500
# 相位 4 的来源文件分页大小,同一房规。
_FILES_PAGE_SIZE = 500
# §1.5's「定稿后处理」#1: form-two's termination condition is `rowcount==0`,
# never `rowcount<batch size` (a concurrent UPDATE can make a batch's
# rowcount smaller than the batch even though rows remain — see
# `_run_form_two`'s docstring). Three consecutive `rowcount==0` rounds
# where a row still demonstrably exists is treated as a loud failure, not a
# silent short-circuit onto the next table.
_FORM_TWO_MAX_STALLED_ROUNDS = 3
# §T-3.3: initial 5s backoff, doubling to a 60s cap.
_QUIESCE_BACKOFF_INITIAL_SECONDS = 5.0
_QUIESCE_BACKOFF_MAX_SECONDS = 60.0
# P1-E (code review): a chronically-failing job gets a bounded retry
# policy, not unbounded sweep-driven ticks. Exponential backoff from the
# most recent failure, capped, then a hard ceiling after which the notebook
# is left in a terminal 'failed' state for an operator to find via
# ``scripts/diag_pg_hotpaths.py``'s notebook_delete_jobs overview (or the
# SQLite-side equivalent direct query) rather than being retried forever.
_MAX_DELETE_ATTEMPTS = 5
_ATTEMPT_BACKOFF_BASE_SECONDS = 60.0
_ATTEMPT_BACKOFF_MAX_SECONDS = 3600.0


def _backoff_seconds(attempts: int) -> float:
    """Attempt 1 waits 60s, attempt 2 waits 120s, ... capped at 3600s."""
    return min(
        _ATTEMPT_BACKOFF_MAX_SECONDS,
        _ATTEMPT_BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1)),
    )


def _parse_timestamp(value: Any) -> datetime | None:
    """Backend-neutral: PostgreSQL hands back a real (UTC-aware)
    ``datetime`` for a ``timestamptz`` column; SQLite hands back an ISO
    string carrying its own explicit local offset (this repository's
    ``now()`` seam is ``datetime.now().astimezone().isoformat(...)`` — see
    ``repository_facade.py``'s module-level ``_now``). Both parse into an
    AWARE ``datetime`` that compares correctly against
    ``datetime.now(timezone.utc)`` regardless of which offset it was
    expressed in — no lexicographic string comparison, unlike
    ``list_stale``'s cutoff (that one is fine because both sides of ITS
    comparison are produced on the same machine at nearly the same moment;
    this one compares a stored past instant against "now" arbitrarily later,
    where that shortcut would not hold)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _artifact_siblings(parent_dir: Path, notebook_id: str) -> list[Path]:
    """One notebook's on-disk siblings under ONE artifact-root parent
    directory (e.g. ``{storage_dir}/kg_index``), in the deletion order
    §T-3b mandates: ``.tmp-<claim_token>`` variants first, then ``.tmp``,
    then ``.old``, then the live directory last.

    P3（PR-3 阶段 B 复查）：「什么算是发布产物目录的 scratch/rollback 兄弟」
    这个判定形态与 ``ScaleArtifactStore.indexed_notebook_ids`` 的排除过滤器
    共享同一对模块常量（``SCRATCH_SUFFIXES``/``SCRATCH_INFIX``，定义在
    ``repositories/filesystem/scale_artifact_store.py``），不再各自维护一份
    字面量——一边改了后缀集合，两边都会同步变化而不是悄悄失配。
    ``test_artifact_siblings_and_indexed_notebook_ids_share_the_same_scratch_
    shape``（``test_notebook_delete_review_fixes.py``）逐一构造边界文件名，
    断言两边分类结果一致。An interrupted publish can leave ANY subset of
    these four present; a healthy notebook typically has only the live one."""
    if not parent_dir.is_dir():
        return []
    old_suffix, tmp_suffix = SCRATCH_SUFFIXES
    tmp_dash: list[Path] = []
    tmp: Path | None = None
    old: Path | None = None
    live: Path | None = None
    tmp_dash_prefix = f"{notebook_id}{SCRATCH_INFIX}"
    for entry in parent_dir.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name
        if name.startswith(tmp_dash_prefix):
            tmp_dash.append(entry)
        elif name == f"{notebook_id}{tmp_suffix}":
            tmp = entry
        elif name == f"{notebook_id}{old_suffix}":
            old = entry
        elif name == notebook_id:
            live = entry
    ordered = sorted(tmp_dash, key=lambda p: p.name)
    for candidate in (tmp, old, live):
        if candidate is not None:
            ordered.append(candidate)
    return ordered


class _NullClaim:
    """Fallback used ONLY when this runner has no ``scale_build_lock`` wired
    at all — a raw unit-test construction of ``NotebookDeleteJobRunner``,
    never ``RepositoryRuntime``'s real wiring (both backends' databases
    always provide ``try_scale_build_lock``). Always reports held, releases
    as a no-op."""

    def verify_held(self) -> bool:
        return True

    def release(self) -> None:
        return None


# Sentinel: the independent per-notebook claim could not be taken THIS
# attempt (held elsewhere, this process's lock-session budget is exhausted,
# the probe itself raised, or — SQLite only — another in-process operation
# already holds it). Distinguished by identity, never by truthiness, same
# convention as ``scale_build_lock.SCALE_BUILD_LOCK_UNAVAILABLE``.
_CLAIM_BUSY = object()


class _ClaimHandle:
    """Wraps a real (or SQLite-sentinel) ``ScaleBuildLock`` handle so
    releasing it ALSO releases this runner's in-process registration
    (P1-B) — the two must rise and fall together or a leaked in-process
    entry would wedge every future claim on this notebook until process
    restart."""

    def __init__(
        self, handle: Any, notebook_id: str,
        unregister: Callable[[str], None] | None,
    ) -> None:
        self._handle = handle
        self._notebook_id = notebook_id
        self._unregister = unregister
        self._released = False

    def verify_held(self) -> bool:
        try:
            return bool(self._handle.verify_held())
        except Exception:  # noqa: BLE001 — an unusable handle reads as "lost"
            return False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._handle.release()
        except Exception:  # noqa: BLE001 — release is best-effort by contract
            pass
        finally:
            if self._unregister is not None:
                try:
                    self._unregister(self._notebook_id)
                except Exception:  # noqa: BLE001 — never let cleanup crash a release
                    pass


class NotebookDeleteJobRunner:
    """Orchestrates one notebook's delete job end to end. Constructed once
    per repository runtime (see ``RepositoryRuntime.wire_knowledge_
    lifecycle``'s tail — that is the earliest point both quiesce legs
    exist)."""

    def __init__(
        self,
        *,
        notebook_store: Any,
        delete_jobs: Any,
        kg_build_jobs: Any,
        kg_maintenance_running: Callable[[str], bool],
        storage_dir: Callable[[], Path],
        analysis_artifacts: Any,
        settings: Any,
        now: Callable[[], str],
        new_id: Callable[[str], str],
        event_log: Any = None,
        scale_build_lock: Callable[[str], ScaleBuildLockAttempt] | None = None,
        scale_artifact_store: Any = None,
        scale_build_claim_in_process: Callable[[str], bool] | None = None,
        scale_build_release_in_process: Callable[[str], None] | None = None,
    ) -> None:
        self.notebook_store = notebook_store
        self.delete_jobs = delete_jobs
        self.kg_build_jobs = kg_build_jobs
        self._kg_maintenance_running = kg_maintenance_running
        self._storage_dir = storage_dir
        self._analysis_artifacts = analysis_artifacts
        self._now = now
        self._new_id = new_id
        self._event_log = event_log
        # 相位 3-5（§T-3b/§4.1/§4.3，P1-B）：与 scale build/W-CLI 复用同一把
        # per-notebook 独占锁——``RepositoryRuntime`` 传入
        # ``database.try_scale_build_lock``（PostgreSQL）或其 SQLite 对等物
        # （无条件放行的哨兵，见 §4.4）。``None`` 只在未接线的测试夹具里出现。
        self._scale_build_lock = scale_build_lock
        self._scale_artifact_store = scale_artifact_store
        # SQLite 进程内互斥（P1-B）：query+register 同一份 ScaleArtifactRuntime
        # 的 ``building`` 集合——删除持有期间一次 build 尝试会看见
        # notebook_id 已在集合里而让路；一次 build 已持有时删除的 register
        # 会失败而落 _CLAIM_BUSY。PostgreSQL 上这两个回调仍会被调用（真会话
        # 锁已经天然互斥，多这一层是无害的冗余），保持 runner 完全不问「我在
        # 哪个后端上」。
        self._scale_build_claim_in_process = scale_build_claim_in_process
        self._scale_build_release_in_process = scale_build_release_in_process
        self._quiesce_timeout_seconds = float(
            getattr(settings, "notebook_delete_quiesce_timeout_seconds", 1800)
        )
        self._sweep_seconds = float(
            getattr(settings, "notebook_delete_sweep_seconds", 300)
        )

    # ------------------------------------------------------------------
    # §T-2: tombstone request — the API-facing entry point
    # ------------------------------------------------------------------

    def request(self, notebook_id: str, actor: str) -> dict:
        """CAS the notebook to 'deleting' + create its delete job row (one
        transaction, §T-2), then submit the background job and return
        immediately. Raises ``KeyError`` (404) / ``NotebookAlreadyDeletingError``
        (409) — the route maps both."""
        job = self.delete_jobs.request(notebook_id, actor)
        self._submit(job["id"], notebook_id)
        return {"status": "deleting"}

    def _submit(self, job_id: str, notebook_id: str) -> None:
        background_jobs.submit(
            self.run, job_id, name=f"deletenb-{notebook_id}"
        )

    # ------------------------------------------------------------------
    # §T-3: the six-phase job, resumed from wherever ``phase`` left off
    # ------------------------------------------------------------------

    def run(self, job_id: str) -> None:
        try:
            job = self.delete_jobs.get(job_id)
        except KeyError:
            # Already finalized (phase 5 deletes this row itself) by a
            # racing/duplicate invocation, or cleaned up out of band.
            return
        if job["status"] not in ("queued", "waiting", "running"):
            return
        # P2-a: mint a fresh lease REGARDLESS of current status — this also
        # covers the case the old code missed entirely: a sweep-driven
        # resubmit of a job that LOOKS 'running' but is actually stale (a
        # worker died without transitioning out of 'running'). The CAS
        # predicate only steals a 'running' row whose updated_at has gone
        # stale; a genuinely alive worker's own heartbeat keeps this branch
        # from ever firing against it (see ``mark_running``'s docstring).
        lease_token = self.delete_jobs.mark_running(
            job_id, stale_cutoff_seconds=self._sweep_seconds,
        )
        if lease_token is None:
            # Lost the race (or the row is 'running' and fresh — someone
            # else genuinely owns it right now).
            return
        notebook_id = job["notebook_id"]
        phase = job["phase"]
        # P1-A: decided ONCE, right after claiming ownership — not
        # re-derived mid-loop. See this module's docstring for the full
        # residual-cleanup rationale.
        residual = not self.delete_jobs.notebook_exists(notebook_id)
        try:
            if phase == "mark":
                self._phase_paths(
                    job_id, notebook_id, lease_token, job.get("cursor_key") or "",
                )
                phase = "paths"
                self.delete_jobs.advance_phase(
                    job_id, phase, lease_token=lease_token, cursor_key="",
                )
            if phase == "paths":
                if not self._phase_quiesce(job_id, notebook_id, lease_token):
                    return  # timed out; mark_waiting already called, sweep resumes
                phase = "quiesce"
                self.delete_jobs.advance_phase(job_id, phase, lease_token=lease_token)
            if phase in ("quiesce", "rows", "files"):
                # P1-B: the independent claim is acquired ONCE here and held
                # through whichever of phases 3/4/5 remain — never
                # re-acquired per phase.
                claim = self._acquire_claim(notebook_id)
                if claim is _CLAIM_BUSY:
                    self.delete_jobs.mark_queued(
                        job_id, lease_token=lease_token,
                        note="相位 3-5 独占锁不可用：被别处持有，或本进程无空余"
                        "锁会话；已置回 queued，交扫尾重试。",
                    )
                    return
                try:
                    if phase == "quiesce":
                        if not self._phase_rows(
                            job_id, notebook_id, lease_token, claim, residual=residual,
                        ):
                            return
                        phase = "rows"
                        self.delete_jobs.advance_phase(
                            job_id, phase, lease_token=lease_token,
                            cursor_table="", cursor_key="",
                        )
                    if phase == "rows":
                        if not self._phase_files(
                            job_id, notebook_id, lease_token, claim, residual=residual,
                        ):
                            return
                        phase = "files"
                        self.delete_jobs.advance_phase(
                            job_id, phase, lease_token=lease_token,
                            cursor_table="", cursor_key="",
                        )
                    if phase == "files":
                        # §4.3: verify_held once more immediately before
                        # phase 5's transaction — the last chance to catch a
                        # lost claim before an irreversible step.
                        if not claim.verify_held():
                            self.delete_jobs.mark_queued(
                                job_id, lease_token=lease_token,
                                note="相位 5 事务前复验丢锁，就地停手，交扫尾重试。",
                            )
                            return
                        if residual:
                            self._finish_residual(job_id, notebook_id)
                        else:
                            self._phase_finalize(job_id, notebook_id)
                finally:
                    claim.release()
        except Exception:  # noqa: BLE001 — must settle the job row, never leave it 'running'
            _log.exception(
                "notebook delete job %s (notebook %s) failed at phase %s",
                job_id, notebook_id, phase,
            )
            self.delete_jobs.finish(
                job_id, "failed",
                error_code="notebook_delete_failed",
                error_message="删除作业执行失败，将由扫尾按退避重试（见§P1-E）。",
            )

    # ---- P1-B: the independent per-notebook claim, spanning phases 3-5 ----

    def _acquire_claim(self, notebook_id: str) -> Any:
        """Returns a claim object (``verify_held``/``release``) or
        ``_CLAIM_BUSY``. Never raises — a probe failure is treated the same
        as "busy" (park and let the sweep retry), matching
        ``scale_artifact_runtime._acquire_scale_build_lock``'s own contract
        for the exact same lock primitive."""
        acquire = self._scale_build_lock
        if acquire is None:
            return _NullClaim()
        try:
            handle = acquire(notebook_id)
        except Exception:  # noqa: BLE001 — an unusable lock backend means "not now"
            _log.exception(
                "notebook delete claim probe failed for %s", notebook_id,
            )
            return _CLAIM_BUSY
        if handle is None or handle is SCALE_BUILD_LOCK_UNAVAILABLE:
            return _CLAIM_BUSY
        register = self._scale_build_claim_in_process
        if register is not None and not register(notebook_id):
            # Another in-process scale build already holds it (P1-B, SQLite
            # mutual exclusion) -- give back whatever real/sentinel handle
            # we just took before reporting busy.
            try:
                handle.release()
            except Exception:  # noqa: BLE001 — release is best-effort by contract
                pass
            return _CLAIM_BUSY
        return _ClaimHandle(handle, notebook_id, self._scale_build_release_in_process)

    # ---- shared: ownership + claim gate for every destructive batch ----

    def _still_owned(self, job_id: str, lease_token: str, *, residual: bool) -> bool:
        """P1-A/P2-a: one query (``ownership_snapshot``) combining "do I
        still own this job row" (status='running' AND matching lease_token)
        with "is the notebook still in the expected state" — 'deleting' in
        the ordinary case, confirmed-still-absent in residual-cleanup mode
        (nothing in this design ever re-creates a hard-deleted notebooks
        row, so that expectation is stable for the lifetime of one run())."""
        snap = self.delete_jobs.ownership_snapshot(job_id)
        if snap is None or snap["status"] != "running" or snap["lease_token"] != lease_token:
            return False
        if residual:
            return True
        return snap["notebook_status"] == "deleting"

    def _batch_ok(
        self, job_id: str, lease_token: str, claim: Any, *, residual: bool,
    ) -> bool:
        """Per-batch gate for every destructive step in phases 3-4 (§4.3's
        "每批 write() 之前调一次 verify_held()", extended to phase 3 by this
        fix): still own the job AND the independent claim is still provably
        held. A lost claim parks the job 'queued' immediately (not merely
        returning False silently) so the sweep does not have to wait out a
        whole extra tick to notice."""
        if not self._still_owned(job_id, lease_token, residual=residual):
            return False
        if not claim.verify_held():
            self.delete_jobs.mark_queued(
                job_id, lease_token=lease_token,
                note="持锁复验失败，就地停手，交扫尾重试。",
            )
            return False
        return True

    # ---- phase 1: paths ----

    def _phase_paths(
        self, job_id: str, notebook_id: str, lease_token: str, start_after: str,
    ) -> None:
        """Materialize the full ``sources.file_path`` set (§T-3.1) before any
        source row can be deleted. Crash-resumable: each page's cursor is
        persisted (as the job's ``cursor_key`` while ``phase`` is still
        'mark') so a resumed run does not re-copy already-materialized
        pages. Runs unconditionally even in residual-cleanup mode: if the
        notebook row is already gone via an out-of-band delete, ``sources``
        has almost certainly already cascaded away too (its FK to
        ``notebooks`` is ``ON DELETE CASCADE``), so this is a harmless
        immediate no-op there — not worth a special case to skip."""
        after_id = start_after
        while True:
            copied, last_id = self.delete_jobs.materialize_paths_page(
                job_id, notebook_id, after_id, _PATHS_PAGE_SIZE
            )
            if copied == 0:
                return
            after_id = last_id or after_id
            self.delete_jobs.advance_phase(
                job_id, "mark", lease_token=lease_token, cursor_key=after_id,
            )

    # ---- phase 2: quiesce ----

    def _phase_quiesce(self, job_id: str, notebook_id: str, lease_token: str) -> bool:
        """Wait for both quiesce legs to clear (§T-3.3): leg A (durable
        ``kg_build_jobs``, covers buildkg-/rebuildkg-) and leg B (in-process
        ``KgMaintenanceJobs``, covers relinkkg-/unifiedkg-). Returns True on
        success (both legs clear), False on timeout (the job is left
        'waiting' for the sweep to resume later — NEVER forced into
        phase 3)."""
        deadline = time.monotonic() + self._quiesce_timeout_seconds
        backoff = _QUIESCE_BACKOFF_INITIAL_SECONDS
        while True:
            leg_a = self.kg_build_jobs.has_running(notebook_id)
            leg_b = self._kg_maintenance_running(notebook_id)
            if not leg_a and not leg_b:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                blocking = []
                if leg_a:
                    blocking.append("durable(kg_build_jobs)")
                if leg_b:
                    blocking.append("in-process(kg_maintenance)")
                note = f"quiesce 超时，仍在等待：{','.join(blocking)}"
                self.delete_jobs.mark_waiting(job_id, lease_token=lease_token, note=note)
                _log.warning(
                    "notebook delete job %s (notebook %s) quiesce timed out "
                    "waiting on: %s",
                    job_id, notebook_id, blocking,
                )
                return False
            # Heartbeat: touch updated_at so the sweep's staleness cutoff
            # (NOTEBOOK_DELETE_SWEEP_SECONDS, often far shorter than
            # NOTEBOOK_DELETE_QUIESCE_TIMEOUT_SECONDS) does not mistake a
            # slow-but-alive quiesce wait for a dead worker and double-submit
            # this same job_id from driver A while this loop is still
            # legitimately polling.
            self.delete_jobs.advance_phase(job_id, "paths", lease_token=lease_token)
            time.sleep(min(backoff, remaining))
            backoff = min(backoff * 2, _QUIESCE_BACKOFF_MAX_SECONDS)

    # ---- phase 3: rows ----

    def _phase_rows(
        self, job_id: str, notebook_id: str, lease_token: str, claim: Any,
        *, residual: bool,
    ) -> bool:
        """Batched cleanup of the 65 closure tables (minus the four
        archive-input tables and ``answers``) + 6 closure-external tables
        (§1.3/§1.5/§T-3), driven by ``notebook_delete_tables.PHASE_3_PLAN``.
        Returns ``True`` once every unit is drained, ``False`` if
        ``_batch_ok`` fails partway through (the job is left exactly where
        it stopped; the caller returns without advancing ``phase``)."""
        row = self.delete_jobs.get(job_id)
        cursor_table = row.get("cursor_table") or ""
        start_index = 0
        if cursor_table:
            try:
                start_index = CURSOR_KEYS.index(cursor_table)
            except ValueError:
                start_index = 0  # unknown cursor_table; restart from the top
        resume_cursor = row.get("cursor_key") or ""
        for index in range(start_index, len(PHASE_3_PLAN)):
            step = PHASE_3_PLAN[index]
            key = CURSOR_KEYS[index]
            after = resume_cursor if index == start_index else ""
            if isinstance(step, DirectTable):
                # §4.4/P2-g: FTS5 shadow cleanup rides alongside its real
                # table's own unit -- a no-op on PostgreSQL, idempotent on
                # every resume.
                self.delete_jobs.delete_fts_shadow(step.table, notebook_id)
                if not self._run_direct_table(
                    job_id, notebook_id, lease_token, claim, key, step, after,
                    residual=residual,
                ):
                    return False
            else:
                assert isinstance(step, Chain)
                if not self._run_chain(
                    job_id, notebook_id, lease_token, claim, key, step.name, after,
                    residual=residual,
                ):
                    return False
        return True

    def _run_direct_table(
        self, job_id: str, notebook_id: str, lease_token: str, claim: Any,
        cursor_key_name: str, step: DirectTable, resume_cursor: str,
        *, residual: bool,
    ) -> bool:
        filter_value = notebook_id
        if step.form == "one":
            cursor = resume_cursor
            while True:
                if not self._batch_ok(job_id, lease_token, claim, residual=residual):
                    return False
                count, last = self.delete_jobs.delete_direct_page_form_one(
                    step.table, step.pk_column, step.filter_column,
                    filter_value, cursor, _ROWS_PAGE_SIZE,
                )
                if count == 0:
                    return True
                cursor = last or cursor
                self.delete_jobs.advance_phase(
                    job_id, "quiesce", lease_token=lease_token,
                    cursor_table=cursor_key_name, cursor_key=cursor,
                    deleted_delta=count,
                )
        return self._run_form_two(
            job_id, notebook_id, lease_token, claim, step.table,
            step.filter_column, filter_value, residual=residual,
        )

    def _run_form_two(
        self, job_id: str, notebook_id: str, lease_token: str, claim: Any,
        table: str, filter_column: str, filter_value: str, *, residual: bool,
    ) -> bool:
        """§1.5's ctid/rowid form. Terminates on ``rowcount == 0``, NEVER on
        ``rowcount < batch size`` — under READ COMMITTED, a row concurrently
        UPDATEd between the inner ``ctid``-collecting SELECT and the DELETE
        loses its old tuple version to PostgreSQL's EPQ re-check, so a
        batch's rowcount can be smaller than the requested limit while rows
        still remain (design doc §1.5's own worked example). Three
        consecutive zero-rowcount rounds where ``table_has_rows`` still
        answers True is treated as a loud failure — the design's defense
        against a concurrent writer this whole tombstone+quiesce design
        assumes is already excluded (a violation of that assumption must
        never surface as a silent skip to the next table)."""
        stalled = 0
        while True:
            if not self._batch_ok(job_id, lease_token, claim, residual=residual):
                return False
            count = self.delete_jobs.delete_direct_batch_form_two(
                table, filter_column, filter_value, _ROWS_PAGE_SIZE,
            )
            if count > 0:
                stalled = 0
                self.delete_jobs.advance_phase(
                    job_id, "quiesce", lease_token=lease_token,
                    deleted_delta=count,
                )
                continue
            if not self.delete_jobs.table_has_rows(table, filter_column, filter_value):
                return True
            stalled += 1
            if stalled >= _FORM_TWO_MAX_STALLED_ROUNDS:
                raise RuntimeError(
                    f"notebook delete phase 3: table {table!r} still has rows "
                    f"for {filter_column}={filter_value!r} after "
                    f"{_FORM_TWO_MAX_STALLED_ROUNDS} consecutive zero-rowcount "
                    "batches (form-two's termination is rowcount==0, not "
                    "rowcount<batch size — see design doc §1.5)"
                )

    def _run_chain(
        self, job_id: str, notebook_id: str, lease_token: str, claim: Any,
        cursor_key_name: str, chain_name: str, resume_cursor: str,
        *, residual: bool,
    ) -> bool:
        """B-class parent-driven chains (§1.3) and the two read-only-parent
        chains (§T-3's ``source_elements``/``ask_trace_steps``) share one
        shape: a store method named ``delete_<chain>_page`` that takes
        one page's worth of the chain's own parent-key cursor, clears the
        WHOLE subtree for that page (P1-D: possibly across several small
        transactions for the two read-only-parent chains, see those store
        methods' own docstrings), and returns ``(page_size, last_key_or_
        None)`` — ``0`` means the chain is drained (for the read-only-parent
        chains this is "no more parent rows", not "no more child rows",
        since a parent with zero children is a legitimate, non-terminal
        state — see those methods' own docstrings on the port)."""
        method = getattr(self.delete_jobs, f"delete_{chain_name}_page")
        cursor = resume_cursor
        while True:
            if not self._batch_ok(job_id, lease_token, claim, residual=residual):
                return False
            count, last = method(notebook_id, cursor, _ROWS_PAGE_SIZE)
            if count == 0:
                return True
            cursor = last or cursor
            self.delete_jobs.advance_phase(
                job_id, "quiesce", lease_token=lease_token,
                cursor_table=cursor_key_name, cursor_key=cursor,
                deleted_delta=count,
            )

    # ---- phase 4: files ----

    def _phase_files(
        self, job_id: str, notebook_id: str, lease_token: str, claim: Any,
        *, residual: bool,
    ) -> bool:
        """Disk-artifact sweep (§T-3b): source files (paged from the
        ``notebook_delete_files`` side table phase 1 materialized), the
        pasted-image asset directory, and the three scale-artifact roots +
        their ``.old``/``.tmp``/``.tmp-<token>`` siblings — all under the
        SAME claim phase 3 already acquired (P1-B: no re-acquisition, no
        gap between phases 3 and 4 an orphan producer tree could slip
        through)."""
        if not self._delete_source_files(job_id, notebook_id, lease_token, claim, residual=residual):
            return False
        self._delete_asset_dir(notebook_id)
        if not self._delete_artifact_roots(
            job_id, notebook_id, lease_token, claim, residual=residual,
        ):
            return False
        return True

    def _delete_source_files(
        self, job_id: str, notebook_id: str, lease_token: str, claim: Any,
        *, residual: bool,
    ) -> bool:
        after_ordinal = -1
        row = self.delete_jobs.get(job_id)
        if (row.get("cursor_table") or "") == "delete_files":
            try:
                after_ordinal = int(row.get("cursor_key") or -1)
            except ValueError:
                after_ordinal = -1
        while True:
            if not self._batch_ok(job_id, lease_token, claim, residual=residual):
                return False
            page = self.delete_jobs.list_files_page(job_id, after_ordinal, _FILES_PAGE_SIZE)
            if not page:
                return True
            for entry in page:
                delete_source_file(entry["file_path"])
            after_ordinal = page[-1]["ordinal"]
            self.delete_jobs.advance_phase(
                job_id, "rows", lease_token=lease_token, cursor_table="delete_files",
                cursor_key=str(after_ordinal),
            )

    def _delete_asset_dir(self, notebook_id: str) -> None:
        _delete_notebook_asset_dir(self._storage_dir(), notebook_id)

    def _delete_artifact_roots(
        self, job_id: str, notebook_id: str, lease_token: str, claim: Any,
        *, residual: bool,
    ) -> bool:
        store = self._scale_artifact_store
        if store is None:
            return True
        root_parents = [
            Path(store.scale_dir(notebook_id)).parent,
            Path(store.viz_dir(notebook_id)).parent,
            Path(store.source_partition_dir(notebook_id)).parent,
        ]
        for parent in root_parents:
            for entry in _artifact_siblings(parent, notebook_id):
                # #643 不变量①：任何破坏性磁盘步骤前复验持锁——丢锁必须就地
                # 停手，绝不继续删同一棵可能已被别的 build/import 接管的树。
                if not self._batch_ok(job_id, lease_token, claim, residual=residual):
                    return False
                shutil.rmtree(entry, ignore_errors=True)
                if entry.exists():
                    _log.warning(
                        "notebook delete job %s (notebook %s): failed to "
                        "remove artifact directory %s (记账不中止, §T-3b)",
                        job_id, notebook_id, entry,
                    )
        return True

    # ---- phase 5: finalize ----

    def _phase_finalize(self, job_id: str, notebook_id: str) -> None:
        """Single transaction (§T-3.2): fence + archive + delete four tables
        (cascading ``answers`` away for free, §1.3's fifth archive
        dependency) + ``DELETE FROM notebooks`` (cascading through the FK
        graph exactly as it does today) + delete this job's own bookkeeping
        rows — all one commit, via ``NotebookStore.delete_row_and_orphan_
        embeddings``'s ``job_id``-bearing extension.

        P3（PR-3 阶段 B 复查）：``delete_row_and_orphan_embeddings`` 仍会返回
        它删掉的 ``sources`` 行各自的 ``file_path``（旧的同步无界删除路径
        ``job_id is None`` 靠这份返回值内联删磁盘文件——那条路径从不跑相位
        4，是它唯一的删文件时机）。**这条 jobized 路径不再需要它**：相位 4
        的 ``_delete_source_files`` 已经在这之前，按 ``notebook_delete_files``
        （相位 1 物化的路径清单）逐页删过同样这些文件、``_delete_asset_dir``
        也已经清过贴图资产目录；相位 3-5 全程持同一把独占 claim（§4.3），
        所以这里不可能出现相位 4 之后又冒出新文件的竞态。以前这里还留着一份
        同样的 ``for file_path in file_paths: delete_source_file(file_path)``
        + ``_delete_notebook_asset_dir(...)`` ——虽然 ``delete_source_file``/
        ``_delete_notebook_asset_dir`` 本身对已经不存在的路径是幂等 no-op，
        不会造成错误，但这是相位 4 引入之前的遗留代码，纯属重复劳动，误导
        读者以为相位 5 还担着磁盘清理的职责——已删除；这里唯一还用得到
        ``delete_row_and_orphan_embeddings`` 返回值的地方就是丢弃它。"""
        self.notebook_store.delete_row_and_orphan_embeddings(
            notebook_id, job_id=job_id
        )
        analysis_artifacts = self._analysis_artifacts
        if analysis_artifacts is not None:
            try:
                analysis_artifacts.redact_notebook(
                    notebook_id, occurred_at=datetime.now(timezone.utc).isoformat()
                )
            except Exception:  # noqa: BLE001 — database deletion already committed
                _log.warning(
                    "analysis artifact redaction failed for %s (%s)",
                    notebook_id, "notebook_delete",
                )

    def _finish_residual(self, job_id: str, notebook_id: str) -> None:
        """§T-4 driver-A's out-of-band-delete special case (P1-A): NO
        notebooks row survives to fence, and the archive projections'
        source tables (``ask_jobs``/``answers``/``sources``/``source_paper_
        meta``/``reports``) all cascade away with whatever out-of-band
        ``DELETE FROM notebooks`` created this state (every one of their FKs
        to ``notebooks`` is ``ON DELETE CASCADE``) — reconstructing a
        partial or empty archive after the fact would be strictly WORSE than
        skipping it (a caller reading ``retained_user_activity`` cannot
        distinguish "genuinely nothing to archive" from "archived
        incompletely because the source rows were already gone"), so this
        NEVER attempts phase 5's fence+archive steps. It only deletes this
        job's own two side-table footprints."""
        self.delete_jobs.finish_residual(job_id)
        _log.info(
            "notebook delete job %s (notebook %s): residual cleanup complete "
            "(driver-A out-of-band-delete special case, §T-4) — no archive "
            "was written or re-written",
            job_id, notebook_id,
        )

    # ------------------------------------------------------------------
    # §T-4: sweep — two drivers, run at startup and every
    # NOTEBOOK_DELETE_SWEEP_SECONDS
    # ------------------------------------------------------------------

    def sweep_once(self) -> int:
        """Driver A: resume stale active job rows (a worker died mid-phase,
        or a quiesce/claim wait parked the row 'waiting'/'queued'). Driver
        B: recreate a missing job row for a 'deleting' notebook with none
        (the CAS committed but the INSERT failed, or the job row was
        deleted out of band) — P1-E: subject to an exponential backoff
        window and a hard attempt ceiling once this notebook has failed
        before. Returns the number of jobs (re)submitted, for callers that
        want to log it."""
        submitted = 0
        for job in self.delete_jobs.list_stale(self._sweep_seconds):
            # §T-4's "作业行在、notebooks 行不在" special case is handled
            # entirely inside run() (P1-A: see this module's docstring) --
            # resubmitting still routes through the SAME phase dispatch, it
            # just takes the residual-cleanup branch instead of the
            # ordinary one.
            self._submit(job["id"], job["notebook_id"])
            submitted += 1
        now = datetime.now(timezone.utc)
        for candidate in self.delete_jobs.list_notebooks_missing_job():
            notebook_id = candidate["notebook_id"]
            attempts = candidate["last_attempts"] or 0
            if attempts >= _MAX_DELETE_ATTEMPTS:
                # P1-E: capped. Leave the most recent failed row as a
                # terminal diagnostic; do not retry again on our own.
                continue
            if attempts > 0:
                finished = _parse_timestamp(candidate["last_finished_at"])
                if finished is not None:
                    elapsed = (now - finished).total_seconds()
                    if elapsed < _backoff_seconds(attempts):
                        continue  # still inside this attempt's backoff window
            if attempts > 0:
                self.delete_jobs.purge_failed_jobs(notebook_id)
            job = self.delete_jobs.recreate_for_deleting_notebook(
                notebook_id, attempts=attempts,
            )
            self._submit(job["id"], notebook_id)
            submitted += 1
        return submitted


class _DeleteSweeper:
    """One daemon thread re-running ``sweep_once`` until stopped. Same shape
    as ``extension_toggles._Refresher`` (that module's own docstring has the
    "wait first" / idempotent-stop rationale this mirrors)."""

    def __init__(self, runner: NotebookDeleteJobRunner, interval_seconds: float) -> None:
        self._runner = runner
        self._interval_seconds = interval_seconds
        self._stop_requested = threading.Event()
        self._started = False
        self._thread = threading.Thread(
            target=self._loop, name="notebook-delete-sweep", daemon=True
        )

    def start(self) -> None:
        self._thread.start()
        self._started = True

    def stop(self) -> None:
        self._stop_requested.set()
        if not self._started:
            return
        if self._thread is threading.current_thread():
            return
        self._thread.join(timeout=5.0)
        if self._thread.is_alive():
            _log.warning(
                "notebook delete sweeper did not stop within 5s; it is a "
                "daemon thread and will exit on its own"
            )

    def _loop(self) -> None:
        while not self._stop_requested.wait(self._interval_seconds):
            try:
                self._runner.sweep_once()
            except Exception:  # noqa: BLE001 — a bad tick must not kill the thread
                _log.exception("notebook delete sweep tick failed")


_active_lock = threading.Lock()
_active: "_DeleteSweeper | None" = None


def start_notebook_delete_sweeper(
    runner: NotebookDeleteJobRunner, interval_seconds: float
) -> Callable[[], None]:
    """Start the periodic sweep thread and return its idempotent stop
    callback. A second start replaces the first (same rationale as
    ``extension_toggles.start_extension_admission_refresher``: only reachable
    via a new repository lifecycle in this process)."""
    if not interval_seconds > 0:
        raise ValueError(f"sweep interval must be positive, got {interval_seconds!r}")
    global _active
    sweeper = _DeleteSweeper(runner, float(interval_seconds))
    with _active_lock:
        previous, _active = _active, None
        if previous is not None:
            previous.stop()
        sweeper.start()
        _active = sweeper

    def _stop() -> None:
        with _active_lock:
            global _active
            if _active is sweeper:
                _active = None
        sweeper.stop()

    return _stop
