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


from app.services.retrieval import score_elements


def test_score_elements_uses_precomputed_sims():
    elements = [
        {"element_id": "e1", "source_id": "s", "element_type": "paragraph",
         "text": "alpha beta", "vector": [1.0, 0.0]},
    ]
    # query 字符串与 "alpha beta" 无 token 重叠 -> keyword=0；语义必须来自
    # 预算 element_sims(0.99)，而非用 vector 重算。融合 = (0.4*0+0.6*0.99)/1.0 = 0.594
    out = score_elements("zzz", elements, query_vector=[1.0, 0.0],
                         element_sims={"e1": 0.99}, limit=8)
    assert out and abs(out[0].score - 0.594) < 0.02
