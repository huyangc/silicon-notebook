"""Filesystem persistence for scale/viz index artifacts (Task 18).

Owns the on-disk layout ({storage_dir}/kg_index/{nb} and
{storage_dir}/kg_viz/{nb}), the manifest reads (full read + the cheap O(1)
version probe), the ScaleIndex/VizIndex load/save delegation and the
temporary/old/live swap sequence — all WITHOUT format changes.

Load/save delegate to the pure ``kg.scale_index`` / ``kg.viz_index`` modules
through their module attributes at call time, so frozen module-level
monkeypatches (the disk-cache suites' ``load_scale_index`` spies) keep
binding, and artifacts written by earlier deploys keep loading (older-index-
stays-valid manifests: has_viz / has_chunk_ann / has_relation_ann absent →
skipped). Path construction keeps the frozen raw ``settings.storage_dir``
semantics (never resolve_path — byte-identical directories).

Locking stays with the caller: ``swap_fold_directory`` only performs the
frozen filesystem sequence (live → .old, tmp → live, rm .old) and is invoked
under the facade's ``_scale_building_lock`` (Task 20 moves that state); a
fold failure before the swap leaves the live artifact untouched. Full
rebuilds (``save_full``) stage through the same .tmp + swap pair, so a
crashed rebuild can no longer leave the live directory half-overwritten.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import threading
from pathlib import Path
from typing import Callable, Mapping, Optional

from app.repositories.scale_build_lock import ScaleBuildLockLost
from app.services.kg import scale_index as scale_index_module
from app.services.kg import viz_index as viz_index_module

ScaleBuildArtifacts = Mapping[str, object]


class ScaleArtifactSwapRefused(RuntimeError):
    """Raised by ``swap_staging_directory`` when it finds a live/.old shape it
    is not safe to clean up on its own (P1, codex PR#643 R9) — currently just
    the recovery state left by a swap that was interrupted between its two
    renames: ``live`` absent, ``.old`` present as the only surviving
    generation. Nothing is renamed or deleted before this is raised; the
    staged build stays exactly where the caller left it.

    A plain ``RuntimeError`` subclass, not a bespoke type nobody expects: every
    existing caller (``save_full``, ``save_source_partitions``, the fold
    builder, the CLI's ``run_import``/``run_build``) that does not specifically
    catch this still gets a readable, un-swallowed failure — the message
    carries the exact recovery command — rather than an exception type its
    generic handlers do not recognize."""


class SwapInterruptGuard:
    """Defer ``SIGINT`` across a rename sequence.

    The renames in ``swap_staging_directory`` are the only steps that destroy a
    published artifact, and they are milliseconds long. A ``KeyboardInterrupt``
    landing between ``live → .old`` and ``tmp → live`` leaves the notebook with
    no live index at all AND escapes the rollback there (which catches
    ``Exception``, not ``BaseException``) — the one window in a build where
    Ctrl-C loses data. Worse, the offline CLI's interrupt cleanup would then
    delete the ``.tmp`` that holds the hours of work just staged. So the signal
    is recorded and re-raised after the sequence completes.

    Lives here, beside the primitive, rather than in the CLI that first grew it:
    a build's publish step is reached only through the store, so the CLI has no
    other seam at which to protect it (codex W-CLI R1 B1).

    Two properties make it safe to install on a production path:

    * off the main thread (the service's build workers) installing a handler is
      impossible, so the guard is a no-op there rather than a crash;
    * nesting is a no-op too — an outer guard (the CLI wraps a whole multi-root
      publish in one) keeps ownership of the deferral, so an inner guard cannot
      chop that sequence into per-root pieces.

    ``reraise=False`` hands the decision back to the caller, which then reads
    ``interrupted`` / ``completed``: an interrupt that arrived while the guarded
    block ran to completion did not stop anything, and reporting it as a failure
    would be a false alarm.
    """

    def __init__(
        self,
        report: Optional[Callable[[str], None]] = None,
        *,
        reraise: bool = True,
    ) -> None:
        self._report = report
        self._reraise = reraise
        self._previous = None
        self._installed = False
        self.interrupted = False
        self.completed = False

    def _handle(self, _signum, _frame) -> None:
        self.interrupted = True
        if self._report is not None:
            self._report(
                "interrupt received during the artifact swap; finishing the "
                "rename sequence first (this takes milliseconds)"
            )

    def __enter__(self) -> "SwapInterruptGuard":
        # Signal handlers can only be installed on the main thread; a worker
        # thread simply gets no masking rather than a crash.
        if threading.current_thread() is not threading.main_thread():
            return self
        previous = signal.getsignal(signal.SIGINT)
        if isinstance(getattr(previous, "__self__", None), SwapInterruptGuard):
            # Already inside somebody else's deferral window; taking it over
            # would end that window early, at this block's exit.
            return self
        if previous is None:
            # ``getsignal`` returns None when the current handler was installed
            # from C, which this cannot put back (``signal.signal(sig, None)``
            # raises). Masking that nobody can undo is worse than not masking.
            return self
        self._previous = previous
        signal.signal(signal.SIGINT, self._handle)
        self._installed = True
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if self._installed:
            signal.signal(signal.SIGINT, self._previous)
            self._installed = False
        self.completed = exc_type is None
        # Only re-raise when the guarded block itself succeeded: a real failure
        # is the more informative one and must not be replaced.
        if self.interrupted and self.completed and self._reraise:
            raise KeyboardInterrupt
        return False


class ScaleArtifactStore:
    def __init__(self, settings) -> None:
        self.settings = settings

    # ─────────────────────────────────────────────────────────── layout ──
    def scale_dir(self, notebook_id: str) -> Path:
        return Path(os.path.join(self.settings.storage_dir, "kg_index", notebook_id))

    def viz_dir(self, notebook_id: str) -> Path:
        return Path(os.path.join(str(self.settings.storage_dir), "kg_viz", notebook_id))

    def source_partition_dir(self, notebook_id: str) -> Path:
        """Companion root; separate so legacy main artifacts stay readable."""
        return Path(
            os.path.join(
                str(self.settings.storage_dir),
                "kg_index_partitions",
                notebook_id,
            )
        )

    def indexed_notebook_ids(self) -> list[str]:
        """Return published scale-index directory names in stable order.

        Only a direct child carrying ``manifest.json`` is a published artifact.
        Atomic-build scratch/rollback directories (``*.tmp`` / ``*.old`` /
        ``*.tmp-<claim_token>``, P1 codex PR#643 R1) are deliberately excluded
        even if an interrupted operator copied a manifest into them.  This is
        a filesystem inventory only; the runtime separately drops orphan
        artifacts whose notebook row no longer exists.
        """
        root = Path(os.path.join(self.settings.storage_dir, "kg_index"))
        if not root.is_dir():
            return []
        return sorted(
            entry.name
            for entry in root.iterdir()
            if entry.is_dir()
            and not entry.name.endswith(".old")
            and not entry.name.endswith(".tmp")
            and ".tmp-" not in entry.name
            and (entry / "manifest.json").is_file()
        )

    # ─────────────────────────────────────────────────── manifest reads ──
    def read_manifest(self, directory) -> Optional[dict]:
        """Full manifest read: missing file → None; a corrupt manifest keeps
        raising (frozen watermark/status semantics). **Valid-but-non-object JSON
        (e.g. ``[]`` / ``"x"`` / ``123``)→ None**:codex 第4轮 P1——那是结构性损坏,
        与「读不出」同款返 None,让 status() 的 ``manifest is None`` 分支把它归为
        stale/corrupt;否则下游 ``manifest.get(...)`` 抛 AttributeError→/index-status 500、
        H8 也漏报,恰好在损坏(最需重建)时够不着重建 CTA。"""
        path = os.path.join(str(directory), "manifest.json")
        if not os.path.exists(path):
            return None
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None

    def manifest_stat_signature(self, directory) -> "tuple | None":
        """``directory/manifest.json`` 的**磁盘身份签名**,或文件不在时 ``None``。

        热路径修复批 2 · R2-5(审计 P1-15):``_stale_manifest_admissible`` 每次
        都用整份 ``read_manifest`` 做身份比对,而生产 manifest 里的
        ``watermark_sources`` 有 48k 个元素(≈2MB JSON);冷路径一次
        ``load(allow_stale=True)`` 要调它两次,一次提问又要 5–10 次
        ``_scale_index(allow_stale=True)`` —— 每次提问 10–20MB 的 JSON 解析,
        只为读出 ``version`` 与 ``pipeline_identity`` 两个字段。

        JSON 没有「只解析头部字段」这回事(``read_manifest_version`` 省的也只是
        返回值,不是解析),把 ``watermark_sources`` 拆成 manifest 旁的独立文件
        是更大的改动(工件格式 + 迁移 + 读写两侧),本批不做。退而求其次:调用方
        按这个签名 memo 解析结果,同一份磁盘工件只解析一次。

        签名 = ``(st_mtime_ns, st_size, st_ino)``。三者都取:
        · 索引发布是 ``.tmp`` 目录 + 原子 rename,换上来的是**另一个 inode**,
          所以 ``st_ino`` 一变就必然重新解析;
        · ``st_mtime_ns`` 是纳秒精度,不是 ``scale_manifest_identity`` 那种
          秒级 mtime 会踩的同秒改写坑;
        · ``st_size`` 再兜一层。
        任何一项变化都强制重新解析,方向保守。``OSError``(含文件不存在)→
        ``None``,调用方按「无工件」处理,与 ``read_manifest`` 的缺失分支同款
        fail-soft。

        ⚠ 前提写清楚(评审 P2-5):``st_mtime_ns`` 的「纳秒精度」是**文件系统的
        属性,不是这个 API 的属性** —— ext4/APFS/XFS 给到亚微秒,而某些网络或
        兼容文件系统(部分 NFS 挂载、FAT 派生)只有秒级甚至 2 秒粒度。签名在那
        种存储上退化成 ``(秒级 mtime, size, inode)``。它仍然是安全的,因为本仓库
        **生产上不存在原地改写 manifest 的写路径**:索引发布一律是「写 ``.tmp``
        目录 + 原子 rename 换目录」,新文件必然是新 inode。粗粒度 mtime 只在
        「同一 inode 上原地改写、且大小不变、且落在同一时间刻度内」时才会漏判,
        而那条路径不存在。真要在这类存储上手工原地编辑 manifest,重启进程即可。
        """
        path = os.path.join(str(directory), "manifest.json")
        try:
            info = os.stat(path)
        except OSError:
            return None
        return (info.st_mtime_ns, info.st_size, info.st_ino)

    def read_manifest_version(self, directory):
        """廉价读 directory/manifest.json 的 version 字段(几 KB,sub-ms)。用于
        allow_stale 检索路径校验「进程缓存里的 stale 实例是否仍是当前磁盘索引」——
        磁盘索引只在 rebuild/fold 时换(新 version),与 kg_mutation_seq 无关。
        文件缺失/损坏/无 version → None(fail-soft,调用方回退到重新 load)。"""
        mpath = os.path.join(str(directory), "manifest.json")
        try:
            with open(mpath) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        # 非对象 JSON(如 [])也算无有效 version:isinstance 守卫,否则 [].get 抛
        # AttributeError→H8 身份路径(scale_manifest_identity)被 checkup 当「探测不确定」
        # 吞掉→漏报损坏、重建 CTA 够不着(codex 第4轮 P1)。fail-soft:返 None,让 H8
        # 继续走磁盘探针(load_scale_index 已有 isinstance 守卫、能正确判 [] 为损坏)。
        return data.get("version") if isinstance(data, dict) else None

    def scale_manifest_identity(self, notebook_id: str) -> "tuple[bool, object]":
        """H8 体检缓存键:磁盘 scale 索引的**产物身份** `(manifest 是否存在, manifest.version)`。

        为什么不是 version_signal:磁盘索引只在 rebuild/fold(`.tmp`+swap 原子换目录)时换新
        `manifest.version`,**与 kg_mutation_seq / version_signal 解耦**(见上 read_manifest_version
        与本类顶部说明)。version_signal 只由 unified_kg_state 的 seq 组成、rebuild/fold 不 bump 它,
        用它当 H8 缓存键会:损坏被缓存后、用户点重建(原子换成健康产物、seq 不变)仍报损坏清不掉。
        改用磁盘 manifest 身份——rebuild/fold 换新 version 即让缓存失效,正是「产物变没变」的真信号。

        `exists=False` → 未建索引(H8 直接判 0、不必 load)。`exists=True, version=None` → manifest 在
        但损坏/无 version(探针会真 load 判损坏,且损坏结论不进缓存,故 None 键不会粘住)。sub-ms。"""
        scale_dir = self.scale_dir(notebook_id)
        manifest_path = os.path.join(str(scale_dir), "manifest.json")
        return os.path.exists(manifest_path), self.read_manifest_version(scale_dir)

    # ──────────────────────────────────────────────────────── load/save ──
    def load_scale(self, notebook_id: str):
        return scale_index_module.load_scale_index(str(self.scale_dir(notebook_id)))

    def load_viz(self, notebook_id: str):
        return viz_index_module.load_viz_index(str(self.viz_dir(notebook_id)))

    def save_viz(self, notebook_id: str, artifacts: Mapping) -> dict:
        # Registered, not fixed (codex W-CLI R1 N3): the ONLINE viz write lands
        # straight in the live directory with no staging, so a crash mid-write
        # can leave a half-written viz root. Its readers already treat a
        # unreadable/absent viz as "no viz" (fail-soft, the graph view degrades
        # to the main index), and the offline ``import`` does publish this root
        # through the tmp+rename primitive; converting the online writer is an
        # artifact-format-adjacent change with its own peak-disk cost and does
        # not belong in this review batch.
        return viz_index_module.save_viz_index(
            str(self.viz_dir(notebook_id)), **artifacts
        )

    def save_full(
        self,
        notebook_id: str,
        artifacts: ScaleBuildArtifacts,
        *,
        claim_token: Optional[str] = None,
        verify_held: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """Full rebuild: stage into a claim-unique {scale_dir}.tmp-*, then
        publish atomically.

        Writing straight into the live directory meant a rebuild that died
        mid-save left the previous manifest.json (written last, so it survived)
        next to half-overwritten arrays — an index that still looked loadable
        but silently described the wrong data. Staging + swap makes the rebuild
        all-or-nothing: a failure anywhere before the swap leaves the previous
        artifact serving untouched, and the abandoned staging directory is
        discarded on the next ``prepare_fold_directory`` call for the SAME
        ``claim_token`` — a different (or absent) token leaves it for
        ``inspect`` (P1, codex PR#643 R1; see ``prepare_staging_directory``).

        Cost: peak disk during a rebuild is roughly two copies of the index
        (GB-scale ANN on large libraries). Fold has always paid exactly this;
        full now does too. There is deliberately no switch to turn it off —
        atomicity is not an option.
        """
        temporary = self.prepare_fold_directory(notebook_id, claim_token)
        manifest = scale_index_module.save_scale_index(str(temporary), **artifacts)
        # The claim is re-verified here rather than at build entry: the staging
        # above is the hours-long part, and a claim proven fresh before it says
        # nothing about who owns the directory now.
        self.swap_fold_directory(notebook_id, temporary, verify_held=verify_held)
        return manifest

    def save_source_partitions(
        self,
        notebook_id: str,
        *,
        parent_version,
        source_ids,
        load_rows,
        claim_token: Optional[str] = None,
        verify_held: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """Build source companions one-at-a-time and atomically publish root.

        A source with incomplete provenance is omitted, not guessed.  The root
        manifest intentionally carries counts only: runtime addresses a
        selected source by SHA-256 and never reads an O(all-sources) directory
        map.  The parent identity is written last and gates every load.
        """
        from app.services.kg.source_partition_index import (
            SOURCE_PARTITION_FORMAT_VERSION,
            SourcePartitionUnavailable,
            build_source_partition,
            save_source_partition,
            source_partition_key,
        )

        live = self.source_partition_dir(notebook_id)
        temporary = self.prepare_staging_directory(live, claim_token)
        published = 0
        unavailable = 0
        for source_id in source_ids:
            try:
                partition = build_source_partition(
                    load_rows(source_id),
                    source_id=source_id,
                    parent_version=parent_version,
                    max_memberships=self.settings.source_subgraph_max_memberships,
                )
            except SourcePartitionUnavailable:
                unavailable += 1
                continue
            save_source_partition(
                temporary / source_partition_key(source_id), partition
            )
            published += 1
        manifest = {
            "format_version": SOURCE_PARTITION_FORMAT_VERSION,
            "parent_version": parent_version,
            "published_sources": published,
            "unavailable_sources": unavailable,
        }
        with open(temporary / "manifest.json", "w") as handle:
            json.dump(manifest, handle, ensure_ascii=False)

        # Same tmp+rename publish as the main root, from the same primitive.
        # ``verify_held`` IS re-checked here, separately from the main swap's
        # own check (codex PR#643 R1 P2): the companion rebuild can run for
        # a long time AFTER the main swap already published, so a claim proven
        # fresh back then says nothing about who owns this directory now. A
        # lost claim raises ``ScaleBuildLockLost`` here exactly as it does for
        # the main root — nothing is renamed and the caller decides how to
        # explain that the main index is already the new generation while the
        # companion is not (see ``_rebuild_source_partitions``).
        self.swap_staging_directory(live, temporary, verify_held=verify_held)
        return manifest

    def load_source_partitions(
        self,
        notebook_id: str,
        source_ids,
        *,
        expected_parent_version,
        expected_source_signatures,
        max_nodes=None,
        max_nnz=None,
    ) -> list:
        """Preflight selected headers, then open only their payloads."""
        from app.services.kg.source_partition_index import (
            SourcePartitionUnavailable,
            inspect_source_partition_manifest,
            load_source_partition,
            validate_partition_root,
        )

        root = self.source_partition_dir(notebook_id)
        validate_partition_root(root, expected_parent_version)
        max_nodes = max_nodes or (
            int(self.settings.source_subgraph_max_objects)
            + int(self.settings.source_subgraph_max_chunks)
            + int(self.settings.source_subgraph_max_cluster_memberships)
        )
        max_nnz = max_nnz or 2 * (
            int(self.settings.source_subgraph_max_relations)
            + int(self.settings.source_subgraph_max_memberships)
            + int(self.settings.source_subgraph_max_cluster_memberships)
        )
        headers = [
            inspect_source_partition_manifest(
                root,
                source_id=source_id,
                expected_parent_version=expected_parent_version,
                expected_source_signature=expected_source_signatures[source_id],
            )[1]
            for source_id in source_ids
        ]
        # n_relations contains only cross-partition endpoints. Reserve both
        # reciprocal transition entries before any large payload is opened.
        if (
            sum(header["n_nodes"] for header in headers) > max_nodes
            or sum(
                header["transition_nnz"] + 2 * header["n_relations"]
                for header in headers
            )
            > max_nnz
        ):
            raise SourcePartitionUnavailable("source_partition_union_limit_exceeded")
        return [
            load_source_partition(
                root,
                source_id=source_id,
                expected_parent_version=expected_parent_version,
                expected_source_signature=expected_source_signatures[source_id],
            )
            for source_id in source_ids
        ]

    # ──────────────────────────────────────────────────────── fold swap ──
    @staticmethod
    def prepare_staging_directory(live, claim_token: Optional[str] = None) -> Path:
        """Reset and hand back ``{live}.tmp-{claim_token}`` for ANY artifact root.

        W-CLI T-W2 generalized this (and ``swap_staging_directory`` below) from
        "the notebook's main scale directory" to "a live directory": an offline
        import publishes THREE roots — the main index, the viz index and the
        source-partition companion — and "main atomic, the rest copied straight
        over the live tree" is not a publishing story anybody can reason about.
        The two notebook-scoped wrappers below keep the original call shape.

        P1, codex PR#643 R1: the staging path used to be the FIXED
        ``{live}.tmp`` regardless of who was building. That let a builder
        whose lock session died mid-build keep writing into ``{live}.tmp``
        while a second process — having legitimately taken over the now-free
        claim — called this same method, ``rmtree``'d the zombie's in-flight
        tree out from under it, and started writing its own data into the
        same directory; both processes' writes could then interleave before
        either swap, and the pre-swap ``verify_held`` re-check only protects
        the RENAME, not the directory the zombie was still filling in. Keying
        the staging path by ``claim_token`` (from the lock handle's session
        identity when there is a real lock, otherwise a random one — no lock
        at all, or a test double) makes every claim's tree its own directory,
        so two claims can never collide on the same path no matter how long a
        zombie keeps writing.

        Only THIS token's own residue is ever cleared here — a retry that
        reuses the same claim_token (the same build attempt, staging again
        after a transient failure) gets a clean directory, but another
        claim's ``{live}.tmp-<other token>`` is left completely alone. The
        one exception is a pre-existing NO-SUFFIX ``{live}.tmp``: that name
        predates this change, so nothing still running could legitimately be
        writing to it, and it is discarded here as a one-time compatibility
        cleanup (the "next build self-heals" contract this method used to
        promise for every leftover, narrowed to just that legacy shape).
        Anything shaped ``{live}.tmp-<token>`` that is not THIS token's own is
        never auto-deleted by anyone — ``inspect`` reports it and an operator
        judges whether it is safe to remove (see docs/operations.md).
        """
        token = claim_token if claim_token else secrets.token_hex(8)
        tmp_dir = f"{live}.tmp-{token}"
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)
        legacy_tmp = f"{live}.tmp"
        if os.path.exists(legacy_tmp):
            shutil.rmtree(legacy_tmp, ignore_errors=True)
        os.makedirs(tmp_dir, exist_ok=True)
        return Path(tmp_dir)

    @staticmethod
    def swap_staging_directory(
        live,
        temporary,
        *,
        verify_held: Optional[Callable[[], bool]] = None,
        keep_old: bool = False,
    ) -> bool:
        """Atomic-swap sequence for ANY artifact root (caller holds the claim):
        live → .old, temporary → live, (rm .old unless ``keep_old``). If
        publishing temporary fails after the first rename, restore .old → live
        before re-raising the original publish error. A failed rollback leaves
        .old intact. Returns ``preserved`` — whether a previous live directory
        existed and was set aside as ``.old`` (a first-ever publish has none).

        ``verify_held`` re-checks the caller's cross-process build claim in the
        last instant before the first rename — the one step that destroys the
        published artifact. A claim that evaporated mid-build (killed lock
        session, failed over database) means another process may already be
        publishing here, so the build is abandoned: nothing is renamed, the
        staged ``.tmp`` is left on disk, and the failure is loud. Callers with
        no cross-process claim (SQLite, direct builder use) pass nothing.

        It is checked TWICE when a stale ``.old`` is found and removed below
        (P1, codex PR#643 R10): the first check, at entry, protects against a
        claim that was already gone before this call started. The pre-clean
        ``rmtree`` of a large stale ``.old`` can itself take tens of seconds,
        long enough for the PostgreSQL lock session backing the claim to die
        mid-delete; a second builder would then legitimately acquire the now-
        free claim and could publish before this call ever reaches its own
        renames. Re-checking ``verify_held`` right after that ``rmtree`` —
        immediately before the guarded block that performs the first rename —
        closes that window: this call abandons the swap instead of renaming
        over whatever the new owner has already published. When there is no
        stale ``.old`` to pre-clean, that second check is skipped entirely —
        nothing slow happens between the entry check and the renames, so
        nothing can invalidate the claim in between, and calling
        ``verify_held`` a second time for no reason would just be a needless
        extra database round trip.

        ``keep_old`` (P2, codex PR#643 R8): when set, the ``.old`` directory is
        left on disk instead of being removed here — the caller has its own
        reason to want it a moment longer (``run_import``'s post-swap identity
        re-check, see ``rollback_swap``/``finalize_swap`` below) and takes
        responsibility for eventually calling one of those two. Every other
        caller (``save_full``, ``save_source_partitions``, the fold builder)
        passes nothing and gets the previous one-call cleanup.

        Before any of that: a stale ``.old`` already on disk (left behind by
        an EARLIER swap, not this one) is dealt with BEFORE the guard is
        entered and BEFORE ``out_dir`` is even read for ``preserved`` (P1/P2,
        codex PR#643 R9). Two shapes are possible:

        * ``live`` present, ``.old`` present — an earlier swap finished both
          its renames but never got to (or lost the race to) delete its own
          leftover ``.old``; that tree is pure cruft and safe to remove.
        * ``live`` ABSENT, ``.old`` present — a previous swap was interrupted
          BETWEEN its two renames (a hard kill, not the deferred Ctrl-C this
          guard exists to prevent) and ``.old`` is the ONLY surviving
          generation. Deleting it here — the old bug — destroys the
          notebook's last artifact before the ``temporary → live`` rename
          that would replace it has even been attempted; if that rename then
          fails, both generations are gone. This shape is refused outright
          with ``ScaleArtifactSwapRefused`` instead: nothing is renamed or
          deleted, and the message tells the operator to restore ``.old``
          onto ``live`` by hand and re-run.

        That pre-clean ``rmtree`` runs with NO ``SwapInterruptGuard`` around
        it (P2, codex PR#643 R9): unlike the two renames below, deleting
        confirmed cruft cannot leave the notebook without a live index —
        ``live``'s presence is checked first — so a Ctrl-C landing during a
        multi-GB delete here should take effect immediately rather than being
        deferred for as long as the rmtree takes. It also runs without
        ``ignore_errors``: a failure here must stop the swap loudly, not
        leave a half-deleted ``.old`` for the renames below to trip over.

        Only the two renames — the steps that can leave a notebook with no live
        index at all — run inside ``SwapInterruptGuard``; deletion of a
        previous generation, whether the pre-clean above or the post-swap
        cleanup below, deliberately runs outside it (P2, codex PR#643 R8/R9):
        by the time either one runs, the notebook already has exactly one
        confirmed-live generation, so a Ctrl-C landing during that
        (potentially multi-GB, tens-of-seconds) delete is safe to honour
        immediately rather than deferring it for as long as the rmtree takes.
        A deferred interrupt that arrived DURING the renames is still
        honoured, just after post-swap cleanup instead of before it —
        ``reraise=False`` on the guard hands that decision to this method
        rather than to ``__exit__``, precisely so cleanup gets a chance to
        run first."""
        out_dir = str(live)
        if verify_held is not None and not verify_held():
            raise ScaleBuildLockLost(
                "scale build lock was lost before the artifact swap for "
                f"{out_dir}; nothing was published and the staged build "
                f"remains at {temporary}"
            )
        old_dir = out_dir + ".old"
        if os.path.exists(old_dir):
            if not os.path.exists(out_dir):
                # Recovery state: a previous swap was interrupted between its
                # two renames and ``.old`` is the ONLY surviving generation.
                # Deleting it here (the old bug, codex PR#643 R9 P1) would
                # destroy the notebook's last artifact before the rename
                # below has even been attempted — refuse instead of guessing.
                raise ScaleArtifactSwapRefused(
                    f"{out_dir} is absent while {old_dir} exists — this "
                    "looks like a swap that was interrupted between its two "
                    f"renames, leaving {old_dir} as the only surviving "
                    f"generation. Restore it first: `mv {old_dir} {out_dir}`, "
                    "then re-run the build. Nothing was renamed or deleted; "
                    f"the staged build remains at {temporary}."
                )
            # ``live`` is present, so this ``.old`` is leftover cruft an
            # earlier swap's own cleanup never finished deleting — safe to
            # remove. No ``SwapInterruptGuard`` here and no ``ignore_errors``
            # — see the docstring above.
            shutil.rmtree(old_dir)
            # P1, codex PR#643 R10: that rmtree can run for tens of seconds on
            # a large stale ``.old`` — long enough for the claim's lock
            # session to die and a second builder to legitimately take over
            # in the meantime. Re-verify right here, before the first rename
            # below, or this call would go on to overwrite the new owner's
            # generation. Only reached on the branch that actually did the
            # slow delete — see the docstring above.
            if verify_held is not None and not verify_held():
                raise ScaleBuildLockLost(
                    "scale build lock was lost before the artifact swap for "
                    f"{out_dir}; nothing was published and the staged build "
                    f"remains at {temporary}"
                )
        guard = SwapInterruptGuard(reraise=False)
        # Everything inside is the destructive part, and Ctrl-C landing inside
        # it is the only way this process can leave a notebook with no live
        # index (the rollback below catches ``Exception``, which a
        # KeyboardInterrupt is not). Deferred, not ignored — see the docstring
        # above and ``SwapInterruptGuard``.
        with guard:
            preserved = os.path.exists(out_dir)
            if preserved:
                os.rename(out_dir, old_dir)
            try:
                os.rename(str(temporary), out_dir)
            except Exception as publish_error:
                if preserved:
                    try:
                        os.rename(old_dir, out_dir)
                    except Exception as rollback_error:
                        publish_error.add_note(
                            "scale artifact rollback failed; previous artifact "
                            f"remains at {old_dir}: {rollback_error!r}"
                        )
                raise
        # A real failure above propagates straight out of the ``with`` block
        # and skips everything below — cleanup only runs once the swap itself
        # is known to have succeeded.
        if not keep_old and preserved:
            shutil.rmtree(old_dir, ignore_errors=True)
        if guard.interrupted:
            raise KeyboardInterrupt
        return preserved

    @staticmethod
    def finalize_swap(live, preserved: bool) -> None:
        """Delete the ``.old`` a ``keep_old=True`` swap left behind, once the
        caller has confirmed the published generation should stand (P1/P2,
        codex PR#643 R8). Never wrapped in ``SwapInterruptGuard``: the new
        generation is already live, so Ctrl-C here is safe to honour
        immediately — a leftover ``.old`` is exactly the shape ``inspect`` and
        the manual-recovery docs already cover.

        Unchanged for ``retire_live_directory`` (codex PR#643 R11 P2-a): a
        retired root's ``.old`` is deleted the exact same way — this method
        never reads a ``temporary`` at all, so the "no replacement tree"
        shape needs nothing new here."""
        if preserved:
            shutil.rmtree(f"{live}.old", ignore_errors=True)

    @staticmethod
    def retire_live_directory(live) -> bool:
        """Publish "no such root" — the degenerate form of a swap that has no
        ``temporary`` to publish in its place (codex PR#643 R11 P2-a).

        A valid import package is allowed to OMIT an optional root
        (``kg_index_partitions``/``kg_viz`` — the switch that produces them can
        be off). Before this existed, an omitted root simply left whatever was
        already live untouched: on a same-version republish the old
        companion's ``parent_version`` still matches, its stat signature never
        changes, and a reader pairs it with the NEW main index — the exact
        "companion describing a different generation" the publish ORDER
        (companion → viz → main) exists to prevent, reached here from a
        different direction (docs/operations.md's generation-pairing
        contract). Retiring the stale root closes that: it degrades to "no
        companion", the same safe shape a first-ever import with no companion
        in the package already produces.

        ``live`` present → a single rename, ``live`` → ``.old`` — the first
        half of ``swap_staging_directory``'s two-rename sequence, with no
        second rename because there is no replacement tree — wrapped in its
        own ``SwapInterruptGuard`` the same way every other rename here is.
        Returns ``True`` (``preserved``, the exact vocabulary ``swap_state``
        already uses in ``run_import``), so a retired root needs no new shape
        in the caller's bookkeeping: ``rollback_swap`` below reverses it with
        ``temporary=None``, and ``finalize_swap`` deletes the ``.old`` it left
        exactly as it does for a real swap.

        ``live`` absent → nothing to retire; returns ``False`` and touches
        nothing. The caller's contract for that case is unchanged: an
        optional root absent from both the package and the live tree stays
        skipped, same as always.
        """
        out_dir = str(live)
        if not os.path.exists(out_dir):
            return False
        old_dir = out_dir + ".old"
        guard = SwapInterruptGuard(reraise=False)
        with guard:
            os.rename(out_dir, old_dir)
        if guard.interrupted:
            raise KeyboardInterrupt
        return True

    @staticmethod
    def rollback_swap(live, temporary, preserved: bool) -> None:
        """Undo a ``keep_old=True`` swap the caller has decided, after the
        fact, must not stand (P1, codex PR#643 R8 — ``run_import``'s post-swap
        pipeline-identity re-check). Two renames, mirroring the original swap
        in reverse: ``live`` (the just-published, now-rejected tree) moves back
        to ``temporary`` — its original staging name, so it is exactly where a
        caller's existing staging-recovery story already expects it — and, if
        ``preserved``, ``.old`` (the previous generation) moves back to
        ``live``. A first-ever publish (``preserved`` False) has no ``.old`` to
        restore: ``live`` is simply absent afterward, same as before that
        publish ever ran. Wrapped in its own ``SwapInterruptGuard`` — this is
        just as destructive as the swap it undoes, and nests transparently
        under a caller's own outer guard the same way ``swap_staging_directory``
        does.

        ``temporary=None`` (codex PR#643 R11 P2-a) reverses
        ``retire_live_directory`` instead of a real swap: a retire published
        "no such root" by renaming ``live`` straight to ``.old`` with nothing
        to replace it, so ``live`` is already absent and there is no
        just-published tree to move back to a staging name — the first rename
        is simply skipped. Only the second rename runs: ``.old`` → ``live``,
        restoring the retired generation exactly as it stood before. Every
        caller that retires a root also only records it in ``swap_state``
        when ``retire_live_directory`` returned ``True``, so ``preserved`` is
        always ``True`` on this path — the branch below still guards on it
        rather than assuming that, for the same reason every other caller of
        this method passes its own ``preserved`` rather than a literal."""
        out_dir = str(live)
        old_dir = out_dir + ".old"
        guard = SwapInterruptGuard(reraise=False)
        with guard:
            if temporary is not None:
                os.rename(out_dir, str(temporary))
            if preserved:
                os.rename(old_dir, out_dir)
        if guard.interrupted:
            raise KeyboardInterrupt

    def prepare_fold_directory(
        self, notebook_id: str, claim_token: Optional[str] = None
    ) -> Path:
        """Staging for BOTH fold and full rebuild (the name predates save_full
        joining; renaming it would churn ownership_manifest.py and the existing
        suites for no functional gain). Resets THIS claim's
        {scale_dir}.tmp-{claim_token} — leftovers from an earlier attempt
        under the SAME token are discarded, a different token's tree is left
        alone (P1, codex PR#643 R1) — and hands it back; the live artifact is
        untouched until swap_fold_directory."""
        return self.prepare_staging_directory(
            self.scale_dir(notebook_id), claim_token
        )

    def swap_fold_directory(
        self,
        notebook_id: str,
        temporary,
        *,
        verify_held: Optional[Callable[[], bool]] = None,
    ) -> None:
        """The notebook's main scale root, published by the primitive above."""
        self.swap_staging_directory(
            self.scale_dir(notebook_id), temporary, verify_held=verify_held
        )
