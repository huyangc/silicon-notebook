from app.core.model_safety import (
    safe_model_error_code,
    safe_model_error_service,
    safe_model_error_stage,
    safe_model_label,
)
from app.models.schemas import ModelError
from app.services import model_status


def test_model_status_and_model_errors_share_one_display_model_sanitizer():
    assert model_status.safe_model_label is safe_model_label


def test_safe_model_label_preserves_namespaces_and_rejects_diagnostics():
    assert safe_model_label("runtime-primary-name") == "runtime-primary-name"
    assert safe_model_label("llama3:70b") == "llama3:70b"
    assert safe_model_label("meta-llama/Llama-3.1-8B-Instruct") == (
        "meta-llama/Llama-3.1-8B-Instruct"
    )
    assert safe_model_label(
        "https://10.0.0.8/v1?api_key=sk-private-secret"
    ) == ""


def test_model_error_metadata_uses_explicit_allowlists():
    assert safe_model_error_service("reasoning_llm") == "reasoning_llm"
    assert safe_model_error_service("private_service") == "llm"
    assert safe_model_error_stage("answer") == "answer"
    assert safe_model_error_stage("private_diagnostic") == "model_call"
    assert safe_model_error_code("missing_config") == "missing_config"
    assert safe_model_error_code("RuntimeError: private response") == "upstream_error"


def test_model_error_schema_defaults_and_legacy_values_are_safe():
    defaulted = ModelError(stage="answer", message="missing_config")
    assert defaulted.model_dump() == {
        "service": "llm",
        "stage": "answer",
        "model": "",
        "message": "missing_config",
    }

    legacy = ModelError(
        service="private_service",
        stage="private_diagnostic",
        model="https://10.0.0.8/v1?api_key=sk-private-secret",
        message="RuntimeError: private response",
    )
    assert legacy.model_dump() == {
        "service": "llm",
        "stage": "model_call",
        "model": "",
        "message": "upstream_error",
    }
