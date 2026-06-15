from app.services.mmr import mmr_rerank


def test_mmr_drops_redundant_for_diverse():
    # a,b 近似重复(两两相似 0.95); c 与众不同(相似 ~0)。
    # 相关度 a>b>c, 但 λ=0.5 下选完 a 后应优先多样的 c, 而非冗余的 b。
    rel = {"a": 0.90, "b": 0.88, "c": 0.70}
    sim = {("a", "b"): 0.95, ("a", "c"): 0.05, ("b", "c"): 0.05}
    def pair(x, y):
        return sim.get((x, y)) or sim.get((y, x)) or (1.0 if x == y else 0.0)
    out = mmr_rerank(["a", "b", "c"], rel, pair, k=2, lambda_=0.5)
    assert out == ["a", "c"]


def test_mmr_pure_relevance_when_lambda_one():
    rel = {"a": 0.5, "b": 0.9, "c": 0.7}
    out = mmr_rerank(["a", "b", "c"], rel, lambda x, y: 0.0, k=3, lambda_=1.0)
    assert out == ["b", "c", "a"]          # 纯按相关度降序


def test_mmr_respects_k_and_handles_short_input():
    rel = {"a": 0.5, "b": 0.9}
    out = mmr_rerank(["a", "b"], rel, lambda x, y: 0.0, k=5, lambda_=0.5)
    assert set(out) == {"a", "b"} and len(out) == 2
