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
            str(self.viz_dir(notebook_id)), **artifacts)

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
        manifest = scale_index_module.save_scale_index(
            str(temporary), **artifacts)
        self.swap_fold_directory(notebook_id, temporary)
        return manifest

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
