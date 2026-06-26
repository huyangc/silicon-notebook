"""回归：带 SILICON_NOTEBOOK_ 前缀的环境变量必须真正生效。

历史 bug：这些字段用 `Field(env="SILICON_NOTEBOOK_...")`，但 pydantic-settings v2
忽略 `env=`，导致设了也没用（静默走默认）。修复=改 `validation_alias`。
顺带覆盖 KG/REWRITE 独立 LLM 端点可经环境变量配置。
"""


def test_silicon_notebook_prefixed_vars_take_effect(monkeypatch):
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV", "production")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", "/data/storage")
    monkeypatch.setenv("SILICON_NOTEBOOK_SINGLE_USER_EMAIL", "ops@example.com")
    monkeypatch.setenv("SILICON_NOTEBOOK_SINGLE_USER_NAME", "Ops")
    from app.core.config import Settings
    s = Settings()
    assert s.environment == "production"
    assert s.storage_dir == "/data/storage"
    assert s.single_user_email == "ops@example.com"
    assert s.single_user_name == "Ops"


def test_kg_llm_env_vars_take_effect(monkeypatch):
    monkeypatch.setenv("KG_LLM_BASE_URL", "http://kg.example/v1")
    monkeypatch.setenv("KG_LLM_API_KEY", "k")
    monkeypatch.setenv("KG_LLM_MODEL", "kg-model")
    from app.core.config import Settings
    s = Settings()
    assert s.kg_llm_base_url == "http://kg.example/v1"
    assert s.kg_llm_configured is True


def test_rewrite_llm_enabled_by_model_only(monkeypatch):
    monkeypatch.setenv("REWRITE_LLM_MODEL", "fast-rewrite")
    from app.core.config import Settings
    s = Settings()
    assert s.rewrite_llm_model == "fast-rewrite"
    assert s.rewrite_llm_configured is True
