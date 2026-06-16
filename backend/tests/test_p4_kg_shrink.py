from app.core.config import Settings


def test_kg_auto_extract_defaults_false():
    assert Settings().kg_auto_extract is False


def test_kg_auto_extract_env_override(monkeypatch):
    monkeypatch.setenv("KG_AUTO_EXTRACT", "true")
    assert Settings().kg_auto_extract is True
