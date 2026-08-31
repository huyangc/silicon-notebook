"""Source-partition companion format version and the main↔companion
generation binding, sunk from app.services.kg.source_partition_index in B3.

``SOURCE_PARTITION_FORMAT_VERSION`` moved here first — the one name
app.repositories (maintenance, both backends) imports directly to compare
against the persisted manifest's ``format_version`` before trusting a
companion CSR partition. The actual partition build/save/load I/O stays in
app.services.kg.source_partition_index (disk-heavy, explicitly out of scope
for that move — see filesystem/scale_artifact_store.py, which keeps
importing the I/O functions from the services module unchanged).
``app.services.kg.source_partition_index`` re-exports both names unchanged
for existing importers.

``new_build_id`` / ``build_generation_mismatch`` join it for the same reason
(P1, codex PR#643 R26): the per-build generation identity is compared against
persisted manifests by the artifact store, by both maintenance backends' cheap
status probe, by the offline CLI and by the partition reader, and a domain
module is the one layer all of them may import.
"""
from __future__ import annotations

import secrets
from typing import Any

SOURCE_PARTITION_FORMAT_VERSION = 2


def new_build_id() -> str:
    """A fresh generation id for ONE build's main manifest.

    Minted per build (full rebuild or delta fold), written to the main
    manifest as ``build_id``, and copied into every companion manifest that
    same build publishes as ``parent_build_id``. Random rather than a
    timestamp: two builds a millisecond apart on two hosts must not be able
    to mint the same id, and a clock that steps backwards must not be able to
    make a new generation look like an old one.
    """
    return secrets.token_hex(16)


def build_generation_mismatch(
    main_build_id: Any, companion_parent_build_id: Any
) -> bool:
    """Is this companion provably from a DIFFERENT build than this main index?

    The pairing gate used to be ``companion.parent_version == main.version``
    alone, and that is not sufficient, because a same-version republish is an
    explicitly supported scenario (P1, codex PR#643 R26). Two reachable
    interruptions leave a mixed pair whose two versions are nevertheless
    equal, so the version-only gate accepted it:

    * a same-version ``import`` interrupted after publishing
      ``kg_index_partitions`` but before ``kg_index`` — the NEW companion now
      sits beside the OLD main index;
    * an online rebuild/fold, which publishes the main root first and the
      companion after — a claim lost in between leaves the NEW main index
      beside the OLD companion.

    Both are supposed to degrade to "no companion" (docs/development.md:37 —
    a mismatched companion is capability-unavailable and never authorizes
    whole-graph post-filtering). The build id is what makes them actually do
    so: one build stamps one id on both roots, so any half-published pair is
    a mismatch no matter what the version numbers say.

    ``True`` only when BOTH sides carry a usable id and the ids differ. A
    side with no id is an artifact built before this key existed, and those
    keep pairing on ``parent_version`` alone (older-index-stays-valid, the
    same rule ``has_viz``/``has_chunk_ann`` get). **Residual, deliberately
    accepted:** such a legacy pair still has the original same-version blind
    spot above — until one new build (full or fold) republishes both roots
    and stamps ids on them, after which the gate is exact. An import package
    carrying legacy roots is the one case where that can be re-introduced
    onto a machine whose live pair already had ids; it publishes both roots
    together, so the imported pair is at least internally consistent.
    """
    main = main_build_id if isinstance(main_build_id, str) else ""
    companion = (
        companion_parent_build_id
        if isinstance(companion_parent_build_id, str)
        else ""
    )
    if not main or not companion:
        return False
    return main != companion
