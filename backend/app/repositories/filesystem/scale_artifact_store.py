"""Filesystem persistence for scale/viz index artifacts (Task 18).

Owns the on-disk layout ({storage_dir}/kg_index/{nb} and
{storage_dir}/kg_viz/{nb}), the manifest reads (full read + the cheap O(1)
version probe), the ScaleIndex/VizIndex load/save delegation and the fold
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
fold failure before the swap leaves the live artifact untouched.
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

    # ─────────────────────────────────────────────────── manifest reads ──
    def read_manifest(self, directory) -> Optional[dict]:
        """Full manifest read: missing file → None; a corrupt manifest keeps
        raising (frozen watermark/status semantics)."""
        path = os.path.join(str(directory), "manifest.json")
        if not os.path.exists(path):
            return None
        with open(path) as fh:
            return json.load(fh)

    def read_manifest_version(self, directory):
        """廉价读 directory/manifest.json 的 version 字段(几 KB,sub-ms)。用于
        allow_stale 检索路径校验「进程缓存里的 stale 实例是否仍是当前磁盘索引」——
        磁盘索引只在 rebuild/fold 时换(新 version),与 kg_mutation_seq 无关。
        文件缺失/损坏/无 version → None(fail-soft,调用方回退到重新 load)。"""
        mpath = os.path.join(str(directory), "manifest.json")
        try:
            with open(mpath) as fh:
                return json.load(fh).get("version")
        except (OSError, ValueError):
            return None

    # ──────────────────────────────────────────────────────── load/save ──
    def load_scale(self, notebook_id: str):
        return scale_index_module.load_scale_index(str(self.scale_dir(notebook_id)))

    def load_viz(self, notebook_id: str):
        return viz_index_module.load_viz_index(str(self.viz_dir(notebook_id)))

    def save_viz(self, notebook_id: str, artifacts: Mapping) -> dict:
        return viz_index_module.save_viz_index(
            str(self.viz_dir(notebook_id)), **artifacts)

    def save_full(self, notebook_id: str, artifacts: ScaleBuildArtifacts) -> dict:
        return scale_index_module.save_scale_index(
            str(self.scale_dir(notebook_id)), **artifacts)

    # ──────────────────────────────────────────────────────── fold swap ──
    def prepare_fold_directory(self, notebook_id: str) -> Path:
        """Fold staging: reset {scale_dir}.tmp (leftovers from an interrupted
        fold are discarded) and hand it back — the live artifact is untouched
        until swap_fold_directory."""
        tmp_dir = str(self.scale_dir(notebook_id)) + ".tmp"
        if os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir)
        os.makedirs(tmp_dir, exist_ok=True)
        return Path(tmp_dir)

    def swap_fold_directory(self, notebook_id: str, temporary) -> None:
        """Atomic-swap sequence (caller holds the building lock):
        live → .old, temporary → live, rm .old. If publishing temporary
        fails after the first rename, restore .old → live before re-raising
        the original publish error. A failed rollback leaves .old intact."""
        out_dir = str(self.scale_dir(notebook_id))
        old_dir = out_dir + ".old"
        if os.path.exists(old_dir):
            shutil.rmtree(old_dir)
        os.rename(out_dir, old_dir)
        try:
            os.rename(str(temporary), out_dir)
        except Exception as publish_error:
            try:
                os.rename(old_dir, out_dir)
            except Exception as rollback_error:
                publish_error.add_note(
                    "scale fold rollback failed; previous artifact remains "
                    f"at {old_dir}: {rollback_error!r}"
                )
            raise
        shutil.rmtree(old_dir, ignore_errors=True)
