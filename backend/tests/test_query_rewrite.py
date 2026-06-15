from app.services.query_rewrite import normalize_terms


def test_splits_letter_digit_boundaries():
    assert normalize_terms("gpt4") == "gpt 4"
    assert normalize_terms("v100 gpu") == "v 100 gpu"
    assert normalize_terms("llama3 vs mistral7b") == "llama 3 vs mistral 7b"


def test_leaves_clean_text_untouched():
    assert normalize_terms("deepseek v2 改进") == "deepseek v2 改进"
    assert normalize_terms("") == ""
