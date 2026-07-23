"""Explicit raw embedding protocol normalization."""
import pytest

from app.core.config import Settings
from app.services.embedding import make_embedder, FakeEmbedder


@pytest.mark.parametrize("provider", ["dashscope", "DashScope", "DASHSCOPE", "  DashScope  "])
def test_make_embedder_accepts_explicit_capitalized_dashscope(provider):
    embedder = make_embedder(
        Settings(_env_file=None),
        provider=provider,
        base_url="https://embedding.example.test",
        api_key="test-key",
        model="text-embedding-v4",
    )
    assert type(embedder).__name__ == "DashscopeEmbedder"


def test_make_embedder_without_explicit_service_is_offline():
    assert isinstance(make_embedder(Settings(_env_file=None)), FakeEmbedder)

