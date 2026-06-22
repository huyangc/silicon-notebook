from app.services.concept_merge_review import _prompt


def test_merge_review_prompt_is_domain_agnostic():
    p = _prompt([{"id": "c1", "score": 0.9, "canonical_a": "MoE",
                  "canonical_b": "Mixture-of-Experts"}])
    low = p.lower()
    assert "cmos" not in low and "rf" not in low and "circuit" not in low
    assert "acronym" in low
    assert "merge" in low and ("keep separate" in low or "keep_separate" in low)
    assert "MoE" in p and "Mixture-of-Experts" in p
