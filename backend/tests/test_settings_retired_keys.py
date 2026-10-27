"""Retired setting keys left in an old deployment's environment are ignored.

``RELATION_SEED_TOP_N`` fed relation hits into graph seeds through a path
that no longer exists, so the setting is gone (S2-E). An ``.env`` written for
an older release, or a process environment that still exports it, must keep
starting.
"""
from __future__ import annotations

from app.core.config import Settings


def test_a_retired_key_in_the_env_file_does_not_stop_startup(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("RELATION_SEED_TOP_N=8\nRELATION_RETRIEVAL_ENABLED=true\n", encoding="utf-8")
    monkeypatch.delenv("RELATION_SEED_TOP_N", raising=False)
    monkeypatch.delenv("RELATION_RETRIEVAL_ENABLED", raising=False)

    settings = Settings(_env_file=env_file)

    assert settings.relation_retrieval_enabled is True  # the file was read
    assert not hasattr(settings, "relation_seed_top_n")


def test_a_retired_key_in_the_process_environment_does_not_stop_startup(monkeypatch):
    monkeypatch.setenv("RELATION_SEED_TOP_N", "8")

    settings = Settings(_env_file=None)

    assert not hasattr(settings, "relation_seed_top_n")
    assert "RELATION_SEED_TOP_N" not in {
        field.validation_alias for field in Settings.model_fields.values()
    }
