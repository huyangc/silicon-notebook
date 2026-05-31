from app.services.extraction_profiles import OBJECT_SCHEMAS, OBJECT_TYPE_LABELS


def test_qiefen_types_registered_with_fields_and_labels():
    assert OBJECT_SCHEMAS["ArticleClaim"].fields == [
        "statement", "problem_addressed", "novelty"]
    assert OBJECT_SCHEMAS["Formula"].fields == [
        "name", "expression", "variables", "applies_to"]
    assert OBJECT_SCHEMAS["Formula"].primary == "name"
    assert "variables" in OBJECT_SCHEMAS["Formula"].list_fields
    assert OBJECT_TYPE_LABELS["Concept"].startswith("概念")
    # legacy types are untouched
    assert "rule" in OBJECT_SCHEMAS and OBJECT_SCHEMAS["rule"].primary == "title"
