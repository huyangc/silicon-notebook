from app.api import system_routes
from app.core.config import Settings
from app.services.model_registry import SystemModelServiceRegistry


def test_health_derives_sanitized_readiness_from_system_registry(monkeypatch):
    settings = Settings(_env_file=None, environment="test", model_services_config="")
    registry = SystemModelServiceRegistry({}, {})
    monkeypatch.setattr(system_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(
        system_routes.SystemModelServiceRegistry,
        "load",
        lambda configured: registry,
    )

    body = system_routes.health()

    assert body == {
        "status": "ok",
        "environment": "test",
        "llm_configured": False,
        "reasoning_llm_configured": False,
        "embedding_configured": False,
    }
