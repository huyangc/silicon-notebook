from harness import metrics


def test_prf_perfect():
    r = metrics.prf(tp=5, fp=0, fn=0)
    assert r["precision"] == 1.0 and r["recall"] == 1.0 and r["f1"] == 1.0


def test_prf_zero_when_empty():
    r = metrics.prf(tp=0, fp=0, fn=0)
    # empty-vs-empty is defined as perfect (nothing to find, nothing wrong)
    assert r["f1"] == 1.0


def test_prf_half():
    r = metrics.prf(tp=1, fp=1, fn=1)
    assert r["precision"] == 0.5 and r["recall"] == 0.5 and r["f1"] == 0.5


def test_jaccard():
    assert metrics.jaccard({1, 2, 3}, {2, 3, 4}) == 0.5
    assert metrics.jaccard(set(), set()) == 1.0
    assert metrics.jaccard({1}, set()) == 0.0
