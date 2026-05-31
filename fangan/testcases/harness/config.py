"""Tunable weights and thresholds — the single place to adjust scoring."""

# Stage buckets; must sum to 1.0. Reflects qiefen emphasis (evidence + extraction).
WEIGHTS = {
    "evidence_atoms": 0.20,
    "semantic_chunks": 0.15,
    "objects": 0.12,          # object existence (type-strict F1)
    "object_payload": 0.13,   # payload-field F1 over loosely-matched objects
    "object_evidence": 0.10,  # local-evidence Jaccard over loosely-matched objects
    "relations": 0.15,
    "context_packages": 0.05,
    "do_not_extract": 0.05,
    "structure": 0.05,        # section_tree + mentions combined
}

THRESHOLDS = {
    "atom_iou": 0.5,        # min source_span IoU to align two atoms
    "chunk_jaccard": 0.5,   # min mapped-atom-set Jaccard to align two chunks
    "object_match": 0.4,    # min composite score to align two objects
    "mention_text": 0.6,    # min text similarity to align two mentions
}

# Composite object-match score = weighted sum of these (must sum to 1.0).
OBJECT_MATCH_WEIGHTS = {"type": 0.4, "evidence": 0.4, "payload": 0.2}
