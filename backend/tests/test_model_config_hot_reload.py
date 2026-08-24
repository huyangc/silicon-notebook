from __future__ import annotations

import threading

from app.core.config import Settings
from app.services import model_provider as provider_module
from app.services.model_provider import RuntimeModelProvider


def _config(*, service_id: str, model: str, top_p: float) -> str:
    return f'''[services.{service_id}]
display_name = "{service_id}"
kind = "chat"
protocol = "openai"
base_url = "https://llm.example/v1"
model = "{model}"
api_key_env = "HOT_RELOAD_KEY"
max_concurrency = 2
top_p = {top_p}

[bindings]
ask_answer = "{service_id}"
'''


class _EventLog:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.reloaded = threading.Event()

    def emit(self, event: dict) -> None:
        self.events.append(event)
        if (
            event.get("kind") == "model_config_reload"
            and event.get("status") == "ok"
            and event.get("service_count") == 1
        ):
            self.reloaded.set()


class _ChatDelegate:
    configured = True

    def __init__(self, service, calls: list[tuple[str, float | None]]) -> None:
        self.service = service
        self.calls = calls
        self.settings = Settings(_env_file=None)
        self.closed = False

    def chat_json(self, messages, response_schema_hint, **kwargs):
        del messages, response_schema_hint, kwargs
        self.calls.append((self.service.model, self.service.top_p))
        return '{"ok":true}'

    def close(self) -> None:
        self.closed = True


def test_background_hot_reload_reroutes_an_existing_workload_adapter(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HOT_RELOAD_KEY", "secret")
    monkeypatch.setattr(
        provider_module, "_MODEL_CONFIG_RELOAD_INTERVAL_SECONDS", 0.01
    )
    path = tmp_path / "model-services.toml"
    path.write_text(
        _config(service_id="first", model="model-one", top_p=1.0),
        encoding="utf-8",
    )
    calls: list[tuple[str, float | None]] = []
    delegates: list[_ChatDelegate] = []

    def factory(service):
        delegate = _ChatDelegate(service, calls)
        delegates.append(delegate)
        return delegate

    events = _EventLog()
    provider = RuntimeModelProvider(
        Settings(
            _env_file=None,
            model_services_config=str(path),
            event_log_enabled=False,
            llm_log_enabled=False,
        ),
        events,
        chat_factory=factory,
    )
    client = provider.chat("ask_answer")
    try:
        assert client.chat_json(
            [{"role": "user", "content": "first"}], "{}"
        ) == '{"ok":true}'

        path.write_text(
            _config(service_id="second", model="model-two", top_p=0.95),
            encoding="utf-8",
        )
        assert events.reloaded.wait(2)

        # The caller keeps the same workload adapter; the call itself resolves
        # the newly published binding and physical-service generation.
        assert provider.chat("ask_answer") is client
        assert client.model == "model-two"
        assert client.chat_json(
            [{"role": "user", "content": "second"}], "{}"
        ) == '{"ok":true}'
        assert calls == [("model-one", 1.0), ("model-two", 0.95)]
    finally:
        provider.close()

    assert delegates and all(delegate.closed for delegate in delegates)


def test_hot_reload_updates_thinking_policy_without_rebuilding_the_service(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HOT_RELOAD_KEY", "secret")
    path = tmp_path / "model-services.toml"
    base = _config(
        service_id="deepseek", model="deepseek-v4-flash", top_p=1.0
    )
    path.write_text(base, encoding="utf-8")
    modes: list[str | None] = []

    class ThinkingDelegate(_ChatDelegate):
        def chat_json(self, messages, response_schema_hint, **kwargs):
            del messages, response_schema_hint
            modes.append(kwargs.get("thinking_mode"))
            return '{"ok":true}'

    provider = RuntimeModelProvider(
        Settings(_env_file=None, model_services_config=str(path)),
        _EventLog(),
        chat_factory=lambda service: ThinkingDelegate(service, []),
    )
    client = provider.chat("ask_answer")
    try:
        client.chat_json([], "{}")
        path.write_text(
            base + '\n[thinking]\nask_answer = "disabled"\n',
            encoding="utf-8",
        )
        assert provider.reload_if_changed(force=True) is True
        assert provider.chat("ask_answer") is client
        client.chat_json([], "{}")
    finally:
        provider.close()

    assert modes == ["enabled", "disabled"]


def test_watcher_waits_for_a_stable_file_signature_before_publish(
    monkeypatch, tmp_path
):
    """A truncate-then-write update must never publish its empty midpoint."""
    monkeypatch.setenv("HOT_RELOAD_KEY", "secret")
    path = tmp_path / "model-services.toml"
    path.write_text(
        _config(service_id="first", model="model-one", top_p=1.0),
        encoding="utf-8",
    )
    provider = RuntimeModelProvider(
        Settings(_env_file=None, model_services_config=str(path)),
        _EventLog(),
        chat_factory=lambda service: _ChatDelegate(service, []),
    )
    try:
        path.write_text("", encoding="utf-8")
        assert provider.reload_if_changed() is False
        assert provider.reload_if_changed() is False
        assert provider.reload_if_changed() is False
        assert provider.chat("ask_answer").model == "model-one"

        path.write_text(
            _config(service_id="second", model="model-two", top_p=0.95),
            encoding="utf-8",
        )
        assert provider.reload_if_changed() is False
        assert provider.chat("ask_answer").model == "model-one"
        assert provider.reload_if_changed() is True
        assert provider.chat("ask_answer").model == "model-two"
    finally:
        provider.close()


def test_watcher_rejects_a_file_that_changes_while_it_is_loaded(monkeypatch, tmp_path):
    """A parsed candidate is publishable only if its post-read signature matches."""
    monkeypatch.setenv("HOT_RELOAD_KEY", "secret")
    path = tmp_path / "model-services.toml"
    path.write_text(
        _config(service_id="first", model="model-one", top_p=1.0),
        encoding="utf-8",
    )
    provider = RuntimeModelProvider(
        Settings(_env_file=None, model_services_config=str(path)),
        _EventLog(),
        chat_factory=lambda service: _ChatDelegate(service, []),
    )
    registry_type = provider_module.SystemModelServiceRegistry
    original_load = registry_type.load
    original_descriptor = registry_type.__dict__["load"]
    try:
        path.write_text(
            _config(service_id="second", model="model-two", top_p=0.95),
            encoding="utf-8",
        )
        assert provider.reload_if_changed() is False

        def _load_then_change(_registry_type, settings):
            candidate = original_load(settings)
            path.write_text(
                _config(service_id="third", model="model-three", top_p=0.9),
                encoding="utf-8",
            )
            return candidate

        monkeypatch.setattr(
            registry_type, "load", classmethod(_load_then_change)
        )
        assert provider.reload_if_changed() is False
        assert provider.chat("ask_answer").model == "model-one"

        monkeypatch.setattr(
            registry_type, "load", original_descriptor
        )
        assert provider.reload_if_changed() is True
        assert provider.chat("ask_answer").model == "model-three"
    finally:
        provider.close()


def test_invalid_hot_reload_keeps_last_valid_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("HOT_RELOAD_KEY", "secret")
    monkeypatch.setattr(
        provider_module, "_MODEL_CONFIG_RELOAD_INTERVAL_SECONDS", 60.0
    )
    path = tmp_path / "model-services.toml"
    path.write_text(
        _config(service_id="first", model="model-one", top_p=1.0),
        encoding="utf-8",
    )
    calls: list[tuple[str, float | None]] = []
    events = _EventLog()
    provider = RuntimeModelProvider(
        Settings(_env_file=None, model_services_config=str(path)),
        events,
        chat_factory=lambda service: _ChatDelegate(service, calls),
    )
    client = provider.chat("ask_answer")
    try:
        path.write_text("[services.invalid\n", encoding="utf-8")

        assert provider.reload_if_changed(force=True) is False
        assert client.model == "model-one"
        assert client.chat_json(
            [{"role": "user", "content": "still-valid"}], "{}"
        ) == '{"ok":true}'
        assert calls == [("model-one", 1.0)]
        assert any(
            event.get("kind") == "model_config_reload"
            and event.get("status") == "error"
            for event in events.events
        )
    finally:
        provider.close()
