"""End-to-end fixture scorer: runs every stage, reusing one atom alignment."""
from . import align, stages, metrics
from .config import THRESHOLDS


def score_fixture(gold, pred, judge=None):
    gold = gold or {}
    pred = pred or {}

    # 1) atoms first — yields the reusable atom alignment
    atoms = stages.score_atoms(gold.get("evidence_atoms"), pred.get("evidence_atoms"))
    atom_p2g = atoms["alignment"]["p2g"]

    # 2) chunks (via atom map)
    chunks = stages.score_chunks(gold.get("semantic_chunks"), pred.get("semantic_chunks"), atom_p2g)

    # 3) objects (via atom map) -> object alignment
    objects = stages.score_objects(gold.get("objects"), pred.get("objects"), atom_p2g, judge=judge)
    obj_g2p = objects["alignment"]["g2p"]
    obj_p2g = objects["alignment"]["p2g"]

    # 4) packages (via chunk + object alignment)
    packages = stages.score_packages(
        gold.get("context_packages"), pred.get("context_packages"), pred.get("objects"),
        chunk_g2p=chunks["alignment"]["g2p"], obj_g2p=obj_g2p,
    )

    # 5) relations (via object alignment)
    relations = stages.score_relations(gold.get("relations"), pred.get("relations"), obj_g2p, obj_p2g)

    # 6) structure (section_tree + mentions)
    structure = stages.score_structure(
        gold.get("section_tree"), pred.get("section_tree"),
        gold.get("mentions"), pred.get("mentions"), atom_p2g,
    )

    # 7) do_not_extract negative control (relative to gold)
    dne = stages.score_do_not_extract(gold.get("do_not_extract"), pred, gold)

    stage_scores = {
        "evidence_atoms": atoms["score"],
        "semantic_chunks": chunks["score"],
        "objects": objects["score"],
        "object_payload": objects["payload"]["f1"],
        "object_evidence": objects["evidence"]["mean_jaccard"],
        "relations": relations["score"],
        "context_packages": packages["score"],
        "do_not_extract": dne["score"],
        "structure": structure["score"],
    }
    weighted = metrics.weighted_total(stage_scores)
    return {
        "weighted_score": weighted,
        "stage_scores": stage_scores,
        "stages": {
            "evidence_atoms": atoms,
            "semantic_chunks": chunks,
            "objects": objects,
            "context_packages": packages,
            "relations": relations,
            "structure": structure,
            "do_not_extract": dne,
        },
        "schema_version": gold.get("schema_version"),
        "profile": (gold.get("source_meta") or {}).get("profile"),
    }
