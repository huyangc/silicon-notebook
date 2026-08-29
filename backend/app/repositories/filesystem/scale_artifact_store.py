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
import shutil
from pathlib import Path
from typing import Mapping, Optional

from app.services.kg import scale_index as scale_index_module
from app.services.kg import viz_index as viz_index_module

ScaleBuildArtifacts = Mapping[str, object]


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
        Atomic-build scratch/rollback directories (``*.tmp`` / ``*.old``) are
        deliberately excluded even if an interrupted operator copied a manifest
        into them.  This is a filesystem inventory only; the runtime separately
        drops orphan artifacts whose notebook row no longer exists.
        """
        root = Path(os.path.join(self.settings.storage_dir, "kg_index"))
        if not root.is_dir():
            return []
        return sorted(
            entry.name
            for entry in root.iterdir()
            if entry.is_dir()
            and not entry.name.endswith((".tmp", ".old"))
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
        return viz_index_module.save_viz_index(
            str(self.viz_dir(notebook_id)), **artifacts
        )

    def save_full(self, notebook_id: str, artifacts: ScaleBuildArtifacts) -> dict:
        """Full rebuild: stage into {scale_dir}.tmp, then publish atomically.

        Writing straight into the live directory meant a rebuild that died
        mid-save left the previous manifest.json (written last, so it survived)
        next to half-overwritten arrays — an index that still looked loadable
        but silently described the wrong data. Staging + swap makes the rebuild
        all-or-nothing: a failure anywhere before the swap leaves the previous
        artifact serving untouched, and the abandoned .tmp is discarded by the
        next prepare_fold_directory.

        Cost: peak disk during a rebuild is roughly two copies of the index
        (GB-scale ANN on large libraries). Fold has always paid exactly this;
        full now does too. There is deliberately no switch to turn it off —
        atomicity is not an option.
        """
        temporary = self.prepare_fold_directory(notebook_id)
        manifest = scale_index_module.save_scale_index(str(temporary), **artifacts)
        self.swap_fold_directory(notebook_id, temporary)
        return manifest

    def save_source_partitions(
        self,
        notebook_id: str,
        *,
        parent_version,
        source_ids,
        load_rows,
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
        temporary = Path(str(live) + ".tmp")
        old = Path(str(live) + ".old")
        if temporary.exists():
            shutil.rmtree(temporary)
        temporary.mkdir(parents=True, exist_ok=True)
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

        if old.exists():
            shutil.rmtree(old)
        preserved = live.exists()
        if preserved:
            os.rename(live, old)
        try:
            os.rename(temporary, live)
        except Exception as publish_error:
            if preserved:
                try:
                    os.rename(old, live)
                except Exception as rollback_error:
                    publish_error.add_note(
                        "source partition rollback failed; previous artifact "
                        f"remains at {old}: {rollback_error!r}"
                    )
            raise
        if preserved:
            shutil.rmtree(old, ignore_errors=True)
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
    def prepare_fold_directory(self, notebook_id: str) -> Path:
        """Staging for BOTH fold and full rebuild (the name predates save_full
        joining; renaming it would churn ownership_manifest.py and the existing
        suites for no functional gain). Resets {scale_dir}.tmp — leftovers from
        an interrupted fold or rebuild are discarded — and hands it back; the
        live artifact is untouched until swap_fold_directory."""
        tmp_dir = str(self.scale_dir(notebook_id)) + ".tmp"
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)
        os.makedirs(tmp_dir, exist_ok=True)
        return Path(tmp_dir)

    def swap_fold_directory(self, notebook_id: str, temporary) -> None:
        """Atomic-swap sequence (caller holds the building lock):
        live → .old, temporary → live, rm .old. If publishing temporary
        fails after the first rename, restore .old → live before re-raising
        the original publish error. A failed rollback leaves .old intact.

        Serves fold AND full rebuild. Fold only ever runs on top of an existing
        index, but a first-ever full rebuild has no live directory yet — so the
        live → .old step is skipped when out_dir is absent. Rollback semantics
        are unchanged: only a live directory that was actually set aside can be
        (and needs to be) put back."""
        out_dir = str(self.scale_dir(notebook_id))
        old_dir = out_dir + ".old"
        if os.path.exists(old_dir):
            shutil.rmtree(old_dir)
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
                        "scale fold rollback failed; previous artifact remains "
                        f"at {old_dir}: {rollback_error!r}"
                    )
            raise
        if preserved:
            shutil.rmtree(old_dir, ignore_errors=True)
