"""回归：带 SILICON_NOTEBOOK_ 前缀的环境变量必须真正生效。

历史 bug：这些字段用 `Field(env="SILICON_NOTEBOOK_...")`，但 pydantic-settings v2
忽略 `env=`，导致设了也没用（静默走默认）。修复=改 `validation_alias`。
模型 endpoint 已由 MODEL_SERVICES_CONFIG 统一管理；这里只保留非模型别名覆盖。
"""
import pytest
from pydantic import ValidationError


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


_ASK_REPORT_BUDGETS = (
    ("ASK_REPORT_CONTEXT_MAX_CHARS", "ask_report_context_max_chars", 12000),
    ("ASK_REPORT_EVIDENCE_BUDGET_CHARS", "ask_report_evidence_budget_chars", 8000),
)


@pytest.mark.parametrize("env_name, field, default", _ASK_REPORT_BUDGETS)
def test_ask_report_budget_defaults_and_alias(monkeypatch, env_name, field, default):
    from app.core.config import Settings

    monkeypatch.delenv(env_name, raising=False)
    assert getattr(Settings(), field) == default

    monkeypatch.setenv(env_name, "4321")
    assert getattr(Settings(), field) == 4321


@pytest.mark.parametrize("env_name, field, default", _ASK_REPORT_BUDGETS)
@pytest.mark.parametrize("value, accepted", [
    ("1999", False),
    ("2000", True),
    ("60000", True),
    ("60001", False),
])
def test_ask_report_budget_bounds(monkeypatch, env_name, field, default, value, accepted):
    from app.core.config import Settings

    monkeypatch.setenv(env_name, value)
    if accepted:
        assert getattr(Settings(), field) == int(value)
    else:
        with pytest.raises(ValidationError) as caught:
            Settings()
        assert env_name in str(caught.value)
