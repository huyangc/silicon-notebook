"""回归：带 SILICON_NOTEBOOK_ 前缀的环境变量必须真正生效。

历史 bug：这些字段用 `Field(env="SILICON_NOTEBOOK_...")`，但 pydantic-settings v2
忽略 `env=`，导致设了也没用（静默走默认）。修复=改 `validation_alias`。
模型 endpoint 已由 MODEL_SERVICES_CONFIG 统一管理；这里只保留非模型别名覆盖。
"""


def test_silicon_notebook_prefixed_vars_take_effect(monkeypatch):
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV", "production")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", "/data/storage")
    monkeypatch.setenv("SILICON_NOTEBOOK_SINGLE_USER_EMAIL", "ops@example.com")
    monkeypatch.setenv("SILICON_NOTEBOOK_SINGLE_USER_NAME", "Ops")
    monkeypatch.setenv("SILICON_NOTEBOOK_ADMIN_PASSWORD", "production-secret")
    from app.core.config import Settings
    s = Settings()
    assert s.environment == "production"
    assert s.storage_dir == "/data/storage"
    assert s.single_user_email == "ops@example.com"
    assert s.single_user_name == "Ops"
