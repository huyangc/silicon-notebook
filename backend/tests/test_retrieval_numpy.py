import math
from app.services.retrieval import cosine, cosine_sims


def test_cosine_sims_matches_cosine():
    q = [0.1, 0.2, 0.3, 0.4]
    vecs = {
        "a": [0.1, 0.2, 0.3, 0.4],
        "b": [0.4, 0.3, 0.2, 0.1],
        "c": [-0.1, -0.2, -0.3, -0.4],
    }
    sims = cosine_sims(q, vecs)
    for k, v in vecs.items():
        assert math.isclose(sims[k], cosine(q, v), abs_tol=1e-6)
    assert math.isclose(sims["a"], 1.0, abs_tol=1e-6)


def test_cosine_sims_empty_and_zero():
    assert cosine_sims([], {"a": [1.0]}) == {}
    assert cosine_sims([1.0, 0.0], {}) == {}
    assert cosine_sims([0.0, 0.0], {"z": [0.0, 0.0]})["z"] == 0.0
