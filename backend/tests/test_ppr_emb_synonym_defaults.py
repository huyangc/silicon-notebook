"""回归：PPR 嵌入同义边默认值及环境变量覆盖。

TB: ppr_emb_synonym_enabled 默认改为 True（保守阈值 0.83），
    PPR_EMB_SYNONYM_ENABLED=false 环境变量能覆盖回 False。
"""


def test_ppr_emb_synonym_enabled_default_true():
    from app.core.config import Settings
    s = Settings(_env_file=None)
    assert s.ppr_emb_synonym_enabled is True, (
        "ppr_emb_synonym_enabled 应默认 True（已启用嵌入跨文档同义边）"
    )


def test_ppr_emb_synonym_threshold_conservative():
    from app.core.config import Settings
    s = Settings(_env_file=None)
    assert s.ppr_emb_synonym_threshold >= 0.83, (
        f"ppr_emb_synonym_threshold 应 ≥0.83 以保守限制误同义边，实际={s.ppr_emb_synonym_threshold}"
    )


def test_ppr_emb_synonym_env_override_disables(monkeypatch):
    monkeypatch.setenv("PPR_EMB_SYNONYM_ENABLED", "false")
    from app.core.config import Settings
    s = Settings(_env_file=None)
    assert s.ppr_emb_synonym_enabled is False, (
        "PPR_EMB_SYNONYM_ENABLED=false 环境变量应能关闭同义边功能"
    )
