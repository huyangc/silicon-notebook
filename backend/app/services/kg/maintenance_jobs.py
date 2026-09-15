"""In-process orchestration for per-notebook KG maintenance jobs.

One INSTANCE owns one per-notebook slot; which job kinds contend for that slot is
decided by which callables the instance is wired with.  Relink and unified
rebuild share one instance because they mutate the same derived graph products;
the editor-facing whole-notebook KG delete joins that same instance because it
removes exactly the graph those two passes read and rewrite.
Conflict detection gets its OWN instance: it writes the conflict review queue,
not those products, so making it wait behind a rebuild (or vice versa) would be
an invented, unhelpful exclusion — it only needs single-flight against itself.

This collaborator owns only claim/status/settlement and worker-entry
orchestration; the algorithms stay owned by the knowledge lifecycle / governance
services and are injected as late-bound callables.
"""
from __future__ import annotations

import threading
from typing import Callable, Dict

from app.core.event_logging import EventLogger
from app.repositories.ports import KgMaintenanceAlreadyRunning


class KgMaintenanceJobs:
    """Coordinate one per-notebook maintenance slot without owning any algorithm."""

    RELINK_COUNTERS = {
        "isolated_before": 0,
        "edges_added": 0,
        "isolated_after": 0,
    }
    REBUILD_COUNTERS = {"clusters": 0}
    DELETE_COUNTERS = {"objects_deleted": 0, "relations_deleted": 0}
    # No "truncated" counter: nothing polls this job's counters (conflict
    # detection has no status endpoint), and the truncation figures are already
    # carried by the run's return value and its counts-only event.
    CONFLICT_COUNTERS = {
        "detected": 0,
        "auto_applied": 0,
        "queued": 0,
    }

    def __init__(
        self,
        *,
        event_log: EventLogger,
        get_notebook: Callable[[str], object],
        new_id: Callable[[str], str],
        relink_notebook_kg: Callable[[str], dict] | None = None,
        rebuild_unified_kg: Callable[[str], int] | None = None,
        resolve_notebook_conflicts: Callable[[str], dict] | None = None,
        delete_notebook_kg: Callable[[str], dict] | None = None,
        kg_build_active: Callable[[str], bool] | None = None,
        durable_build_running: Callable[[str], bool] | None = None,
        cross_admission_lock: "threading.Lock | None" = None,
        require_notebook: Callable[[str], object] | None = None,
    ) -> None:
        # The algorithm callables are per-instance and optional: an instance is
        # wired only with the kinds that must share its slot, and calling a job
        # entry it was not wired with is a programming error, not a runtime mode.
        self.event_log = event_log
        self.get_notebook = get_notebook
        # status() 的存在性检查:只需「活着的笔记本行在不在」(缺失/墓碑即
        # KeyError→404),不需要 get_notebook 拼出的整份 NotebookSummary——
        # 打开笔记本会连发几条状态 GET,删除进行中还按秒级轮询。未接线时退回
        # get_notebook,语义相同只是更贵。claim 仍走 get_notebook(一次点击
        # 一次,不在轮询路径上)。
        self._require_notebook = require_notebook or get_notebook
        self._new_id = new_id
        self._relink_notebook_kg = relink_notebook_kg
        self._rebuild_unified_kg = rebuild_unified_kg
        self._resolve_notebook_conflicts = resolve_notebook_conflicts
        # 删除知识图谱的算法入口(返回 {表名: 删除行数})。只接给 relink/rebuild
        # 共用槽的实例:删的正是那两趟要读、要重写的图。
        self._delete_notebook_kg = delete_notebook_kg
        # 批 3·W2 §2.1(buildkg- × unifiedkg-/relinkkg- 交叉检查):探测
        # 「该笔记本是否有 buildkg-/rebuildkg- 作业在飞」。只接给
        # relink/rebuild 共用槽的实例——冲突检测不碰派生簇/板块产物,与
        # build 无互斥关系。
        self._kg_build_active = kg_build_active
        # 持久分析行探测(kg_build_jobs 里该笔记本是否有 running 行)。进程内
        # kg_building 标记看不见**别的进程**(与后端并跑的 batch_ingest kg
        # CLI)建的作业;删除知识图谱会排空那个作业正在抽取的图,所以只接给
        # 删除的准入(claim 成功后、仲裁锁之外探测——内存锁里不做数据库 I/O)。
        self._durable_build_running = durable_build_running
        # 仲裁锁(codex #673 R2 P2):把「登记自己 + 查对方」整段对 build
        # 准入侧原子化——纯 write-then-check 的对开会双双退让,两个调用方
        # 都拿 409 而没有任何作业在跑。同一把锁也包住 prepare/standalone
        # delete 的「占 kg_building + 查维护槽」,先进临界区者完整胜出。
        self._cross_admission_lock = cross_admission_lock

        # Terminal entries remain available for the browser's next bounded poll.
        # Every kind wired into this instance intentionally shares this registry
        # and lock. This stays
        # separate from durable kg_build_jobs: relink/rebuild are maintenance
        # passes, and publishing them as extraction jobs would claim the wrong
        # single-flight domain and expose false 0/0 analysis progress. Production
        # runs one worker, so process-local ownership is the deployment contract;
        # after restart an honest idle response replaces the vanished entry.
        self.jobs: Dict[str, dict] = {}
        self.lock = threading.Lock()

    def claim(
        self, notebook_id: str, kind: str, id_prefix: str, counters: Dict[str, int],
        *, exempt_build_marker: bool = False,
        durable_build_probe: Callable[[str], bool] | None = None,
    ) -> dict:
        """Claim before submission so racing clicks cannot enqueue two writers.

        ``exempt_build_marker``:build 作业自己的收尾(``_relink_after_build``)
        在 build 仍持 ``kg_building`` 标记时顺序调进来——它看见的标记就是
        **自己的**,交叉检查不该把自己的收尾闸死(§2.1 防的是独立维护动作
        撞上在飞 build)。只有 build 收尾传 True;外部入口一律 False。

        ``durable_build_probe``:槽登记成功后、**仲裁锁之外**再查一次持久
        running 分析行(内存临界区里不做数据库 I/O)。命中即按与标记检查
        相同的纪律撤销刚登记的槽(放回被顶掉的终态条目)并按
        holder="buildkg" 拒绝;探测本身抛错同样先撤销再上抛。探测之后才
        落地的持久行(另一进程在这之后建的作业)不在此列——与槽本身一样
        只是进程内保证。"""
        self.get_notebook(notebook_id)
        from contextlib import nullcontext
        arbitration = self._cross_admission_lock or nullcontext()
        with arbitration:
            job, displaced = self._claim_arbitrated(
                notebook_id, kind, id_prefix, counters,
                exempt_build_marker=exempt_build_marker)
        if durable_build_probe is not None:
            try:
                durable_running = durable_build_probe(notebook_id)
            except BaseException:
                self._revoke_claim(notebook_id, job["job_id"], displaced)
                raise
            if durable_running:
                self._revoke_claim(notebook_id, job["job_id"], displaced)
                raise KgMaintenanceAlreadyRunning(notebook_id, "buildkg")
        return dict(job)

    def _revoke_claim(
        self, notebook_id: str, job_id: str, displaced: "dict | None"
    ) -> None:
        """Undo a refused claim while it still owns the slot.

        恢复被顶掉的终态条目而不是 del(质量评 P3:浏览器的最后一次 bounded
        poll 不该因为一次被拒的 claim 把刚完成的统计读成 idle/0)。"""
        with self.lock:
            current = self.jobs.get(notebook_id)
            if current is not None and current["job_id"] == job_id:
                if displaced is not None:
                    self.jobs[notebook_id] = displaced
                else:
                    del self.jobs[notebook_id]

    def _claim_arbitrated(
        self, notebook_id: str, kind: str, id_prefix: str,
        counters: Dict[str, int], *, exempt_build_marker: bool,
    ) -> "tuple[dict, dict | None]":
        with self.lock:
            current = self.jobs.get(notebook_id)
            if current is not None and current["status"] == "running":
                raise KgMaintenanceAlreadyRunning(notebook_id, current["kind"])
            displaced = current   # 终态槽条目——被拒时要放回去,不是删掉
            job = {
                "job_id": self._new_id(id_prefix),
                "notebook_id": notebook_id,
                "kind": kind,
                "status": "running",
                **counters,
            }
            self.jobs[notebook_id] = job
        # 批 3·W2 §2.1:先登记后查对方(两侧同为 write-then-check,Dekker
        # 序保证至少一方看见另一方——先查后登记会留下双双放行的窗口)。
        # build 在飞即撤销刚登记的槽并按 holder="buildkg" 拒绝,409 文案
        # 点名真正占着的动作。
        if (not exempt_build_marker
                and self._kg_build_active is not None
                and self._kg_build_active(notebook_id)):
            self._revoke_claim(notebook_id, job["job_id"], displaced)
            raise KgMaintenanceAlreadyRunning(notebook_id, "buildkg")
        return job, displaced

    def active_kind(self, notebook_id: str) -> "str | None":
        """正在跑的维护种类(无则 None)——build 侧交叉检查用(§2.1)。"""
        with self.lock:
            current = self.jobs.get(notebook_id)
            if current is not None and current["status"] == "running":
                return str(current["kind"])
            return None

    def status(
        self, notebook_id: str, kind: str, counters: Dict[str, int]
    ) -> dict:
        """Return this kind's state, treating another kind's claim as idle."""
        self._require_notebook(notebook_id)
        with self.lock:
            job = self.jobs.get(notebook_id)
            if job is None or job["kind"] != kind:
                return {
                    "job_id": "",
                    "notebook_id": notebook_id,
                    "status": "idle",
                    "running": False,
                    **counters,
                }
            return {
                "job_id": job["job_id"],
                "notebook_id": job["notebook_id"],
                "status": job["status"],
                "running": job["status"] == "running",
                **{name: job[name] for name in counters},
            }

    def settle(
        self,
        notebook_id: str,
        job_id: str,
        status: str,
        stats: dict | None = None,
    ) -> None:
        """Publish terminal state only while this exact job still owns the slot."""
        with self.lock:
            job = self.jobs.get(notebook_id)
            if job is None or job["job_id"] != job_id:
                return
            job["status"] = status
            if stats:
                for name in job:
                    if name in ("job_id", "notebook_id", "kind", "status"):
                        continue
                    if name in stats:
                        job[name] = int(stats[name])

    def start_notebook_relink(
        self, notebook_id: str, *, exempt_build_marker: bool = False
    ) -> dict:
        return self.claim(
            notebook_id, "relink", "rlj", dict(self.RELINK_COUNTERS),
            exempt_build_marker=exempt_build_marker,
        )

    def notebook_relink_status(self, notebook_id: str) -> dict:
        return self.status(notebook_id, "relink", dict(self.RELINK_COUNTERS))

    def run_notebook_relink_job(self, notebook_id: str, job_id: str) -> dict:
        """Run relink and settle on every exit, including ``BaseException``."""
        try:
            stats = self._relink_notebook_kg(notebook_id)
        except Exception:
            self.settle(notebook_id, job_id, "failed")
            self.event_log.logger.exception(
                "relink_notebook_kg failed for %s", notebook_id
            )
            self.event_log.emit({
                "kind": "kg_relink_failed",
                "notebook_id": notebook_id,
            })
            raise
        except BaseException:
            self.settle(notebook_id, job_id, "failed")
            raise
        self.settle(notebook_id, job_id, "succeeded", stats)
        return stats

    def fail_notebook_relink_submission(
        self, notebook_id: str, job_id: str
    ) -> None:
        self.settle(notebook_id, job_id, "failed")

    def start_unified_kg_rebuild(self, notebook_id: str) -> dict:
        return self.claim(
            notebook_id, "rebuild", "ukj", dict(self.REBUILD_COUNTERS)
        )

    def unified_kg_rebuild_status(self, notebook_id: str) -> dict:
        return self.status(notebook_id, "rebuild", dict(self.REBUILD_COUNTERS))

    def run_unified_kg_rebuild_job(self, notebook_id: str, job_id: str) -> int:
        """Run the version-gated rebuild and settle on every exit."""
        try:
            clusters = int(self._rebuild_unified_kg(notebook_id))
        except KgMaintenanceAlreadyRunning:
            # 批 3·W2(质量评 P2):数据级代际闸的拒绝在这里冒出来时,进程内
            # 槽已被本作业占下、202 已返回——占号的是**另一进程**(离线 CLI
            # 直连)的在飞认领。这是「被闸」不是「真失败」:作业仍落 failed
            # 终态(没有别的诚实终态),但事件与日志用被闸语义,不发
            # unified_kg_rebuild_failed 误导运维去查故障。
            self.settle(notebook_id, job_id, "failed")
            self.event_log.logger.info(
                "rebuild_unified_kg gated by an in-flight derived-generation "
                "claim for %s (cross-process single-flight)", notebook_id
            )
            self.event_log.emit({
                "kind": "unified_kg_rebuild_gated",
                "notebook_id": notebook_id,
            })
            raise
        except Exception:
            self.settle(notebook_id, job_id, "failed")
            self.event_log.logger.exception(
                "rebuild_unified_kg failed for %s", notebook_id
            )
            self.event_log.emit({
                "kind": "unified_kg_rebuild_failed",
                "notebook_id": notebook_id,
            })
            raise
        except BaseException:
            self.settle(notebook_id, job_id, "failed")
            raise
        self.settle(
            notebook_id, job_id, "succeeded", {"clusters": clusters}
        )
        return clusters

    def fail_unified_kg_rebuild_submission(
        self, notebook_id: str, job_id: str
    ) -> None:
        self.settle(notebook_id, job_id, "failed")

    # --- whole-notebook KG delete (same slot as relink/rebuild) ---------------

    def start_kg_delete(self, notebook_id: str) -> dict:
        # 不带 exempt_build_marker:删除没有「自己的 build 收尾」这回事,
        # 分析在飞一律按 holder="buildkg" 拒绝——进程内标记之外还查持久
        # running 行,挡住与后端并跑的 CLI 分析作业。
        return self.claim(
            notebook_id, "delete", "kdj", dict(self.DELETE_COUNTERS),
            durable_build_probe=self._durable_build_running,
        )

    def kg_delete_status(self, notebook_id: str) -> dict:
        return self.status(notebook_id, "delete", dict(self.DELETE_COUNTERS))

    def run_kg_delete_job(self, notebook_id: str, job_id: str) -> dict:
        """Run the KG delete and settle on every exit, including ``BaseException``.

        The stats published to the status view are counts only, mapped from the
        delete's ``{table: rows_deleted}`` return; a table the delete did not
        report counts as zero."""
        try:
            counts = self._delete_notebook_kg(notebook_id)
        except Exception:
            self.settle(notebook_id, job_id, "failed")
            self.event_log.logger.exception(
                "delete_notebook_kg failed for %s", notebook_id
            )
            self.event_log.emit({
                "kind": "kg_delete_failed",
                "notebook_id": notebook_id,
            })
            raise
        except BaseException:
            self.settle(notebook_id, job_id, "failed")
            raise
        stats = {
            "objects_deleted": int((counts or {}).get("knowledge_objects", 0)),
            "relations_deleted": int(
                (counts or {}).get("knowledge_relations", 0)
            ),
        }
        self.settle(notebook_id, job_id, "succeeded", stats)
        self.event_log.emit({
            "kind": "kg_deleted",
            "notebook_id": notebook_id,
            **stats,
        })
        return stats

    def fail_kg_delete_submission(self, notebook_id: str, job_id: str) -> None:
        self.settle(notebook_id, job_id, "failed")

    # --- conflict detection (its OWN instance's slot — see module docstring) --

    def start_conflict_resolution(self, notebook_id: str) -> dict:
        return self.claim(
            notebook_id, "conflict", "cfj", dict(self.CONFLICT_COUNTERS)
        )

    def run_conflict_resolution_job(self, notebook_id: str, job_id: str) -> dict:
        """Run conflict resolution and settle on every exit, incl. ``BaseException``."""
        try:
            stats = self._resolve_notebook_conflicts(notebook_id)
        except Exception:
            self.settle(notebook_id, job_id, "failed")
            self.event_log.logger.exception(
                "resolve_notebook_conflicts failed for %s", notebook_id
            )
            self.event_log.emit({
                "kind": "kg_conflict_resolution_failed",
                "notebook_id": notebook_id,
            })
            raise
        except BaseException:
            self.settle(notebook_id, job_id, "failed")
            raise
        self.settle(notebook_id, job_id, "succeeded", stats)
        return stats

    def fail_conflict_resolution_submission(
        self, notebook_id: str, job_id: str
    ) -> None:
        self.settle(notebook_id, job_id, "failed")
