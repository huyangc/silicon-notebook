"""Auto user-memory suggestions — contracts, Settings, wiring predicates (plan T1).

PostgreSQL twin of the identity read-side fallback lives in
``tests/postgres/test_core_store_conformance.py``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.core.request_context import set_request_user
from app.domain import memory_suggest_contracts as contracts
from app.models.identity import MemoryModeUpdate
from app.services.memory_suggest_wiring import (
    memory_suggest_enabled_signals,
    memory_suggest_model_ready,
    memory_suggest_wiring_active,
)
from app.services.sqlite_repository import SQLiteRepository

_BUDGET_FIELDS = (
    "AUTO_MEMORY_SUGGEST_TRIGGER",
    "AUTO_MEMORY_SUGGEST_BACKLOG_MAX",
    "AUTO_MEMORY_SUGGEST_DAILY_CAP",
    "AUTO_MEMORY_SUGGEST_ITEMS_PER_RUN",
    "AUTO_MEMORY_SUGGEST_CANDIDATES_PER_RUN",
    "AUTO_MEMORY_SUGGEST_QUESTION_MAX_CHARS",
    "AUTO_MEMORY_SUGGEST_CLARIFICATION_MAX_CHARS",
    "AUTO_MEMORY_SUGGEST_ANSWER_EXCERPT_MAX_CHARS",
    "AUTO_MEMORY_SUGGEST_INPUT_MAX_CHARS",
    "AUTO_MEMORY_SUGGEST_OUTPUT_MAX_TOKENS",
    "AUTO_MEMORY_SUGGEST_EXPIRY_DAYS",
    "AUTO_MEMORY_SUGGEST_DECLINED_TTL_DAYS",
    "AUTO_MEMORY_SUGGEST_EXPIRY_SWEEP_LIMIT",
)


@pytest.fixture(autouse=True)
def _clear_request_user():
    yield
    set_request_user(None)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def test_defaults_match_the_approved_budgets():
    settings = Settings(_env_file=None)

    assert settings.auto_memory_suggest_enabled is True
    assert settings.auto_memory_suggest_clarified_enabled is True
    assert settings.auto_memory_suggest_repeated_enabled is True
    assert settings.auto_memory_suggest_corrected_enabled is True
    assert (
        settings.auto_memory_suggest_trigger,
        settings.auto_memory_suggest_backlog_max,
        settings.auto_memory_suggest_daily_cap,
        settings.auto_memory_suggest_items_per_run,
        settings.auto_memory_suggest_candidates_per_run,
        settings.auto_memory_suggest_question_max_chars,
        settings.auto_memory_suggest_clarification_max_chars,
        settings.auto_memory_suggest_answer_excerpt_max_chars,
        settings.auto_memory_suggest_input_max_chars,
        settings.auto_memory_suggest_output_max_tokens,
        settings.auto_memory_suggest_expiry_days,
        settings.auto_memory_suggest_declined_ttl_days,
        settings.auto_memory_suggest_expiry_sweep_limit,
    ) == (5, 5, 3, 8, 3, 500, 500, 1500, 8000, 1024, 30, 30, 500)


@pytest.mark.parametrize("alias", _BUDGET_FIELDS)
def test_every_budget_rejects_zero(alias):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{alias: "0"})


# --------------------------------------------------------------------------- #
# Wiring predicates
# --------------------------------------------------------------------------- #
def _flags(enabled=True, clarified=True, repeated=True, corrected=False):
    return SimpleNamespace(
        auto_memory_suggest_enabled=enabled,
        auto_memory_suggest_clarified_enabled=clarified,
        auto_memory_suggest_repeated_enabled=repeated,
        auto_memory_suggest_corrected_enabled=corrected,
    )


def test_enabled_signals_follow_their_own_switches():
    assert memory_suggest_enabled_signals(_flags()) == {"clarified", "repeated"}
    assert memory_suggest_enabled_signals(_flags(clarified=False)) == {"repeated"}
    assert memory_suggest_enabled_signals(_flags(repeated=False)) == {"clarified"}
    assert memory_suggest_enabled_signals(_flags(clarified=False, repeated=False)) == set()


def test_wiring_needs_the_deployment_switch_and_one_signal_class():
    assert memory_suggest_wiring_active(_flags()) is True
    assert memory_suggest_wiring_active(_flags(enabled=False)) is False
    assert memory_suggest_wiring_active(_flags(clarified=False)) is True
    assert (
        memory_suggest_wiring_active(_flags(clarified=False, repeated=False))
        is False
    )


def test_a_settings_object_without_the_flags_reads_as_off():
    assert memory_suggest_enabled_signals(SimpleNamespace()) == set()
    assert memory_suggest_wiring_active(SimpleNamespace()) is False
    assert memory_suggest_wiring_active(
        SimpleNamespace(auto_memory_suggest_clarified_enabled=True)
    ) is False


def test_model_ready_reads_the_memory_suggest_binding_and_fails_closed():
    seen: list[str] = []

    def configured(workload_id: str) -> bool:
        seen.append(workload_id)
        return True

    assert memory_suggest_model_ready(SimpleNamespace(configured=configured)) is True
    assert seen == ["memory_suggest"]
    assert memory_suggest_model_ready(None) is False
    assert memory_suggest_model_ready(SimpleNamespace(configured=lambda _w: False)) is False

    def broken(_workload_id: str) -> bool:
        raise RuntimeError("provider down")

    assert memory_suggest_model_ready(SimpleNamespace(configured=broken)) is False


# --------------------------------------------------------------------------- #
# Contracts
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("suggest", "suggest"),
        ("manual", "manual"),
        ("auto", "manual"),
        ("", "manual"),
        (None, "manual"),
        ("SUGGEST", "manual"),
    ],
)
def test_normalize_memory_mode_folds_unknown_values_to_manual(stored, expected):
    assert contracts.normalize_memory_mode(stored) == expected


def test_memory_mode_update_validates_the_mode_and_defaults_delete_off():
    assert MemoryModeUpdate(memory_mode="suggest").delete_unadopted is False
    assert MemoryModeUpdate(memory_mode="manual", delete_unadopted=True).delete_unadopted
    with pytest.raises(ValidationError):
        MemoryModeUpdate(memory_mode="auto")
    with pytest.raises(ValidationError):
        MemoryModeUpdate(memory_mode="manual", extra=True)


def test_run_event_keys_never_carry_content():
    assert not (
        contracts.MEMORY_SUGGEST_RUN_EVENT_KEYS
        & contracts.MEMORY_SUGGEST_FORBIDDEN_EVENT_KEYS
    )
    assert {"question", "answer", "title", "content", "statement", "tags"} <= (
        contracts.MEMORY_SUGGEST_FORBIDDEN_EVENT_KEYS
    )


def test_failure_outcomes_are_terminal_outcomes():
    assert contracts.FAILURE_OUTCOMES <= set(contracts.JOB_OUTCOMES)
    assert contracts.SUGGESTED_SIGNALS == ("clarified", "repeated", "corrected")
    assert contracts.JOB_STATUSES == ("idle", "running")


# --------------------------------------------------------------------------- #
# Identity read side (SQLite)
# --------------------------------------------------------------------------- #
def _repo(tmp_path) -> SQLiteRepository:
    return SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path}/t.db",
            storage_dir=str(tmp_path / "storage"),
            _env_file=None,
            event_log_enabled=False,
            llm_log_enabled=False,
        )
    )


@pytest.mark.parametrize("stored", ["auto", "", "SUGGEST", "manual"])
def test_unknown_memory_mode_reads_back_as_manual(tmp_path, stored):
    repo = _repo(tmp_path)
    identity = repo._runtime.identity
    user = identity.create_user("z00000011", "pw")
    with repo._runtime.database.write() as db:
        db.execute(
            "UPDATE user_profiles SET memory_mode = ? WHERE user_id = ?",
            (stored, user.id),
        )

    assert identity.authenticate_user("z00000011", "pw").memory_mode == "manual"

    with repo._runtime.database.write() as db:
        db.execute(
            "UPDATE user_profiles SET memory_mode = 'suggest' WHERE user_id = ?",
            (user.id,),
        )
    assert identity.authenticate_user("z00000011", "pw").memory_mode == "suggest"


# --------------------------------------------------------------------------- #
# GET /system/config
# --------------------------------------------------------------------------- #
def _client(tmp_path, monkeypatch, **env: str) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/system-config.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    from app.api import deps
    from app.main import create_app

    deps.repository.cache_clear()
    return TestClient(create_app())


def _config(client: TestClient) -> dict:
    response = client.post(
        "/api/auth/register", json={"username": "z00123456", "password": "pw"}
    )
    assert response.status_code == 200, response.text
    headers = {"Authorization": f"Bearer {response.json()['token']}"}
    config = client.get("/api/system/config", headers=headers)
    assert config.status_code == 200, config.text
    return config.json()


def test_system_config_reports_the_switch_and_an_unbound_model(tmp_path, monkeypatch):
    payload = _config(_client(tmp_path, monkeypatch))

    assert payload["auto_memory_suggest_enabled"] is True
    # The offline test runtime binds no model at all.
    assert payload["auto_memory_suggest_model_ready"] is False
    get_settings.cache_clear()


@pytest.mark.parametrize(
    "env",
    [
        {"AUTO_MEMORY_SUGGEST_ENABLED": "false"},
        {
            "AUTO_MEMORY_SUGGEST_CLARIFIED_ENABLED": "false",
            "AUTO_MEMORY_SUGGEST_REPEATED_ENABLED": "false",
            "AUTO_MEMORY_SUGGEST_CORRECTED_ENABLED": "false",
        },
    ],
)
def test_system_config_reports_the_switch_off(tmp_path, monkeypatch, env):
    payload = _config(_client(tmp_path, monkeypatch, **env))

    assert payload["auto_memory_suggest_enabled"] is False
    get_settings.cache_clear()


def test_system_config_reports_a_bound_model(tmp_path, monkeypatch):
    from app.api import system_routes

    monkeypatch.setattr(
        system_routes,
        "model_provider_if_initialized",
        lambda: SimpleNamespace(configured=lambda workload: workload == "memory_suggest"),
    )
    payload = _config(_client(tmp_path, monkeypatch))

    assert payload["auto_memory_suggest_model_ready"] is True
    get_settings.cache_clear()
