from app.services.retrieval import relation_embed_text


def test_relation_embed_text_combines_fields():
    t = relation_embed_text("Regulated Cascode", "derived_from", "Cascode",
                            ["adds a gain stage to boost output resistance"])
    assert "Regulated Cascode" in t and "Cascode" in t
    assert "derived_from" in t
    assert "gain stage" in t


def test_relation_embed_text_truncates_evidence():
    t = relation_embed_text("A", "supports", "B", ["x" * 1000], max_evidence_chars=50)
    # evidence 截断到 50;头部 "A —supports→ B." 不计入截断额度
    assert t.count("x") <= 50


def test_relation_embed_text_handles_empty_evidence():
    t = relation_embed_text("A", "about", "B", [])
    assert t == "A —about→ B."
