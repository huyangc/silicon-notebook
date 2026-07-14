"""EMBED_PROVIDER 大小写不敏感:品牌名 DashScope 常被写成驼峰,代码不应因此当成未配置。"""
import pytest

from app.core.config import Settings
from app.services.embedding import make_embedder, FakeEmbedder


@pytest.fixture
def embed_env(monkeypatch):
    """配齐 EMBED 四件套里的 base/key/model,只把 provider 的大小写留给各用例。"""
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "text-embedding-v4")
    return monkeypatch


@pytest.mark.parametrize("provider", ["dashscope", "DashScope", "DASHSCOPE", "  DashScope  "])
def test_embedder_configured_is_case_insensitive(embed_env, provider):
    embed_env.setenv("EMBED_PROVIDER", provider)
    assert Settings().embedder_configured is True


@pytest.mark.parametrize("provider", ["", "openai", "bge"])
def test_embedder_not_configured_for_empty_or_unsupported(embed_env, provider):
    embed_env.setenv("EMBED_PROVIDER", provider)
    assert Settings().embedder_configured is False


def test_make_embedder_accepts_capitalized_dashscope(embed_env):
    embed_env.setenv("EMBED_PROVIDER", "DashScope")
    assert type(make_embedder(Settings())).__name__ == "DashscopeEmbedder"


