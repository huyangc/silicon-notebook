"""PostgreSQL KG lexical 自适应路由的部署配置合同。"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_knn_max_term_chars_default_and_env_override(monkeypatch):
    monkeypatch.delenv("POSTGRES_LEXICAL_KNN_MAX_TERM_CHARS", raising=False)
    assert Settings(_env_file=None).postgres_lexical_knn_max_term_chars == 32

    monkeypatch.setenv("POSTGRES_LEXICAL_KNN_MAX_TERM_CHARS", "48")
    assert Settings(_env_file=None).postgres_lexical_knn_max_term_chars == 48


@pytest.mark.parametrize("value", ["2", "257", "not-an-integer"])
def test_knn_max_term_chars_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("POSTGRES_LEXICAL_KNN_MAX_TERM_CHARS", value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
