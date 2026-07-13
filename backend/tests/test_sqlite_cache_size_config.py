from app.core.config import Settings


def test_sqlite_cache_size_default(monkeypatch):
    monkeypatch.delenv("SQLITE_CACHE_SIZE_KB", raising=False)
    assert Settings().sqlite_cache_size_kb == -16384


def test_sqlite_cache_size_env_override(monkeypatch):
    monkeypatch.setenv("SQLITE_CACHE_SIZE_KB", "-8192")
    assert Settings().sqlite_cache_size_kb == -8192
