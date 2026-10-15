import pytest
from pydantic import ValidationError

from app.core.config import Settings


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", ""),
        ("http://10.0.0.5:3000", "http://10.0.0.5:3000"),
        ("https://nb.example.com/", "https://nb.example.com"),
        ("https://nb.example.com/sub/path//", "https://nb.example.com/sub/path"),
    ],
)
def test_valid(monkeypatch, raw, expected):
    monkeypatch.setenv("PUBLIC_WEB_BASE_URL", raw)
    assert Settings().public_web_base_url == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://nb.example.com/?a=1",
        "https://nb.example.com/#x",
        "https://u:p@nb.example.com",
        "https://u@nb.example.com",
        "ftp://nb.example.com",
        "nb.example.com",
        "https:///path",
        "https://nb.example.com/a b",
        "https://nb.example.com/\x07",
    ],
)
def test_rejected(monkeypatch, raw):
    monkeypatch.setenv("PUBLIC_WEB_BASE_URL", raw)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize(
    "env,default,ok,bad",
    [
        ("MCP_ANSWER_LINK_TTL_HOURS", 168, (1, 720), (0, 721)),
        ("MCP_ANSWER_LINK_GRACE_HOURS", 168, (0, 720), (-1, 721)),
        ("MCP_ANSWER_PREVIEW_MAX_CHARS", 300, (40, 2000), (39, 2001)),
    ],
)
def test_integer_bounds(monkeypatch, env, default, ok, bad):
    attr = env.lower()
    monkeypatch.delenv(env, raising=False)
    assert getattr(Settings(), attr) == default
    for v in ok:
        monkeypatch.setenv(env, str(v))
        assert getattr(Settings(), attr) == v
    for v in bad:
        monkeypatch.setenv(env, str(v))
        with pytest.raises(ValidationError):
            Settings()
