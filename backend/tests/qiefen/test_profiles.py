from app.services.qiefen.profiles import (
    object_types, relation_types, extraction_targets,
)


def test_article_vocab():
    objs = object_types("article_research")
    assert objs["ArticleClaim"] == ["statement", "problem_addressed", "novelty"]
    assert "ScalingLaw" in objs
    assert "result_supports_claim" in relation_types("article_research")
    # extraction_targets are the object type names
    assert extraction_targets("article_research")[0] == "ArticleClaim"
    assert set(extraction_targets("article_research")) == set(objs.keys())


def test_textbook_vocab():
    objs = object_types("textbook")
    assert objs["Formula"] == ["name", "expression", "variables", "applies_to"]
    assert "DesignPrinciple" in objs
    assert "formula_used_in_example" in relation_types("textbook")
    assert set(extraction_targets("textbook")) == set(objs.keys())
