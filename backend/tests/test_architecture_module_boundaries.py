def test_retired_personal_model_routes_and_raw_probes_are_absent():
    from pathlib import Path

    from app.main import app

    retired = {
        "/api/me/model-settings",
        "/api/me/model-settings/test",
        "/api/me/model-services/status",
        "/api/me/model-services/test-all",
        "/api/me/model-services/{service}/test",
    }
    assert retired.isdisjoint(app.openapi()["paths"])

    source = (
        Path(__file__).resolve().parents[1] / "app" / "api" / "system_routes.py"
    ).read_text(encoding="utf-8")
    for retired_fragment in (
        "/me/model-settings",
        "/me/model-services",
        "OpenAICompatibleClient",
        "RerankClient(",
        "get_user_model_settings",
        "patch_user_model_settings_atomic",
    ):
        assert retired_fragment not in source
