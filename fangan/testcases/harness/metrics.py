"""P/R/F1, Jaccard, and weighted aggregation."""
from . import config


def prf(tp, fp, fn):
    if tp == 0 and fp == 0 and fn == 0:
        return {"tp": 0, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0, "f1": 1.0}
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall, "f1": f1}


def jaccard(a, b):
    a, b = set(a), set(b)
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def weighted_total(stage_scores):
    """stage_scores: {bucket_name: score_in_0_1}. Returns 0..100.

    Buckets absent from stage_scores are treated as perfect (1.0) so the total
    stays comparable; callers always supply all WEIGHTS keys in practice.
    """
    total = 0.0
    for bucket, w in config.WEIGHTS.items():
        total += w * float(stage_scores.get(bucket, 1.0))
    return round(100.0 * total, 2)
