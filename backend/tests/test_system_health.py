from app.api import system_routes
from app.api import deps
from app.core.config import Settings


def test_health_binding_summary_reads_runtime_registry_without_provider_traffic(monkeypatch):
    class Models:
        def __init__(self):
            self.calls = []

        def configured(self, workload_id):
            self.calls.append(workload_id)
            return workload_id != "reasoning_agent"

        def probe(self, *_args, **_kwargs):
            raise AssertionError("health must never probe a model service")

    models = Models()
    runtime = type("Runtime", (), {"models": models})()
    facade = type("Facade", (), {"_runtime": runtime})()
    monkeypatch.setattr(deps, "repository", lambda: facade)

    summary = deps.model_service_binding_summary()

    assert summary == {
        "llm_configured": True,
        "reasoning_llm_configured": False,
        "embedding_configured": True,
    }
    assert models.calls == [
        "ask_answer",
        "reasoning_agent",
        "retrieval_query_embedding",
    ]


def test_health_derives_sanitized_readiness_from_system_registry(monkeypatch):
    settings = Settings(_env_file=None, environment="test", model_services_config="")
    monkeypatch.setattr(system_routes, "get_settings", lambda: settings)

    body = system_routes.health({
        "llm_configured": False,
        "reasoning_llm_configured": False,
        "embedding_configured": False,
    })

    assert body == {
        "status": "ok",
        "environment": "test",
        "llm_configured": False,
        "reasoning_llm_configured": False,
        "embedding_configured": False,
    }
