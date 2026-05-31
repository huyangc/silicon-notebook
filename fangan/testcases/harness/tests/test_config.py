from harness import config


def test_weights_sum_to_one():
    assert abs(sum(config.WEIGHTS.values()) - 1.0) < 1e-9


def test_thresholds_present():
    for k in ("atom_iou", "chunk_jaccard", "object_match"):
        assert 0.0 <= config.THRESHOLDS[k] <= 1.0


def test_object_match_weights_sum_to_one():
    assert abs(sum(config.OBJECT_MATCH_WEIGHTS.values()) - 1.0) < 1e-9
