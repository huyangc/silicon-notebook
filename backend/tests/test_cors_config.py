"""CORS 来源从环境变量（逗号串）正确解析的回归测试。

历史 bug：`cors_origins` 用 `Field(env="SILICON_NOTEBOOK_CORS_ORIGINS")`，但
pydantic-settings v2 忽略 `env=`，导致该环境变量失效（部署时 CORS 配不上）。
修复=`validation_alias` + `NoDecode`（避免把 List 字段的逗号串当 JSON 解析）。
"""


def test_cors_origins_from_env_comma_list(monkeypatch):
    monkeypatch.setenv(
        "SILICON_NOTEBOOK_CORS_ORIGINS",
        "http://a.example:3000,http://b.example:3000",
    )
    from app.core.config import Settings
    s = Settings()
    assert s.cors_origins == ["http://a.example:3000", "http://b.example:3000"]


def test_cors_origins_strips_whitespace(monkeypatch):
    monkeypatch.setenv(
        "SILICON_NOTEBOOK_CORS_ORIGINS",
        " http://a.example:3000 , http://b.example:3000 ",
    )
    from app.core.config import Settings
    s = Settings()
    assert s.cors_origins == ["http://a.example:3000", "http://b.example:3000"]


def test_cors_origins_single_value(monkeypatch):
    monkeypatch.setenv("SILICON_NOTEBOOK_CORS_ORIGINS", "http://only.example:3000")
    from app.core.config import Settings
    s = Settings()
    assert s.cors_origins == ["http://only.example:3000"]
