from app.services.retrieval import est_tokens, truncate_by_tokens


def test_est_tokens():
    assert est_tokens("") == 0 and est_tokens("abcd") >= 1


def test_truncate_keeps_prefix():
    items = ["x" * 40, "y" * 40, "z" * 40]
    kept = truncate_by_tokens(items, key=lambda s: s, max_tokens=20)
    assert 0 < len(kept) < 3
