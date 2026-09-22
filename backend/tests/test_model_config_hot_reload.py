from __future__ import annotations

import threading

from app.core.config import Settings
from app.services import model_provider as provider_module
from app.services.model_provider import RuntimeModelProvider
from app.services.model_registry import WORKLOADS


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


def _settings(path) -> Settings:
    """Hot-reload settings with the startup binding gate off.

    These tests are about the WATCHER — stable-signature debouncing, rerouting
    an existing adapter, keeping the last valid registry when a candidate is
    rejected. Their fixtures bind one workload on purpose so the reroute is
    observable; a complete 37-workload table would add nothing but noise.
    Binding completeness is enforced at startup and covered by
    ``test_model_registry``; the reload path shares the same ``load`` and the
    same gate, which ``test_strict_reload_is_rejected_and_keeps_the_previous_registry``
    below pins explicitly.
    """
    return Settings(
        _env_file=None,
        model_services_config=str(path),
        model_bindings_strict=False,
    )


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
            model_bindings_strict=False,  # 同 _settings:这里测的是 watcher
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
        service_id="chat", model="gateway-model-alias", top_p=1.0
    )
    path.write_text(base, encoding="utf-8")
    modes: list[str | None] = []

    class ThinkingDelegate(_ChatDelegate):
        def chat_json(self, messages, response_schema_hint, **kwargs):
            del messages, response_schema_hint
            modes.append(kwargs.get("thinking_mode"))
            return '{"ok":true}'

    provider = RuntimeModelProvider(
        _settings(path),
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
        _settings(path),
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
        _settings(path),
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
        _settings(path),
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


def _complete_config(*, model: str) -> str:
    """A config that satisfies the strict startup gate: every workload bound."""
    services = "\n".join((
        _config(service_id="chat", model=model, top_p=1.0).split("[bindings]")[0],
        '[services.embed]\ndisplay_name = "embed"\nkind = "embedding"\n'
        'protocol = "dashscope"\nbase_url = "https://embed.example"\n'
        'model = "embed-model"\napi_key_env = "HOT_RELOAD_KEY"\n'
        "max_concurrency = 2\n",
        '[services.rerank]\ndisplay_name = "rerank"\nkind = "rerank"\n'
        'protocol = "dashscope"\nbase_url = "https://rerank.example"\n'
        'model = "rerank-model"\napi_key_env = "HOT_RELOAD_KEY"\n'
        "max_concurrency = 2\n",
    ))
    by_kind = {"chat": "chat", "embedding": "embed", "rerank": "rerank"}
    bindings = "\n".join(
        f'{workload_id} = "{by_kind[workload.kind]}"'
        for workload_id, workload in sorted(WORKLOADS.items())
    )
    return f"{services}\n[bindings]\n{bindings}\n"


def test_strict_reload_is_rejected_and_keeps_the_previous_registry(
    monkeypatch, tmp_path
):
    """热重载走同一把闸:少一行绑定的新文件被拒,旧注册表原样留着。

    PR-4 的严格校验做在 ``SystemModelServiceRegistry.load`` 里,所以启动与热重
    载共用同一条判据。线上编辑 ``model-services.toml`` 时删错一行,服务不会带着
    半张绑定表继续跑。

    诊断的落点要说准:整句 ``model-bindings: ...`` 只进**进程日志**
    (``reload_if_changed`` 里的 ``logger.error``);``model_config_reload`` 事件
    刻意只带 ``status=error`` 与 ``code=invalid_configuration``,不带消息正文。
    目前也没有任何管理端接口去触发 reload——watcher 与强制 reload 是仅有的两个
    入口,所以「运维看得到原因」的唯一依据就是那条日志。
    """
    monkeypatch.setenv("HOT_RELOAD_KEY", "secret")
    monkeypatch.setattr(
        provider_module, "_MODEL_CONFIG_RELOAD_INTERVAL_SECONDS", 60.0
    )
    path = tmp_path / "model-services.toml"
    path.write_text(_complete_config(model="model-one"), encoding="utf-8")
    events = _EventLog()
    provider = RuntimeModelProvider(
        Settings(
            _env_file=None,
            model_services_config=str(path),
            model_bindings_strict=True,
            event_log_enabled=False,
            llm_log_enabled=False,
        ),
        events,
        chat_factory=lambda service: _ChatDelegate(service, []),
    )
    try:
        assert provider.chat("ask_answer").model == "model-one"

        path.write_text(
            _complete_config(model="model-two").replace(
                'kg_glean = "chat"\n', ""
            ),
            encoding="utf-8",
        )
        assert provider.reload_if_changed(force=True) is False
        assert provider.chat("ask_answer").model == "model-one"
        assert provider.registry.service_for("kg_glean") is not None
        assert any(
            event.get("kind") == "model_config_reload"
            and event.get("status") == "error"
            for event in events.events
        )
    finally:
        provider.close()
