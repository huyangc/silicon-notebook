import pytest

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


@pytest.mark.parametrize(
    "unsafe_model",
    [
        "example.com",
        "service.local",
        "例子.测试",
        "example.xn--p1ai",
        "api.example.com",
        "models.acme.internal",
        "api.例子.测试",
        "api.example.com/v1",
        "vendor/api.example.com/v1",
        "api.example.xn--p1ai/v1",
        "api.example.com./v1",
        "user@api.example.com/v1",
        "api.例子.测试/v1",
        "api._service.local/v1",
        "AKIAIOSFODNN7EXAMPLE",
        "vendor/AKIAIOSFODNN7EXAMPLE",
        "AKIAIOSFODNN7EXAMPLE/latest",
        "ASIAIOSFODNN7EXAMPLE",
        "sk_live_51H8xabcdefghijkl",
        "vendor/sk_live_51H8xabcdefghijkl",
        "pk_test_51H8xabcdefghijkl",
        "alice:s3cr3t@api.example.com",
        "vendor/alice:s3cr3t@api.example.com",
    ],
)
def test_safe_model_label_rejects_endpoint_and_credential_shapes(unsafe_model):
    assert safe_model_label(unsafe_model) == ""


@pytest.mark.parametrize(
    "safe_model",
    [
        "01-ai/Yi-1.5-9B-Chat",
        "runtime-primary-name",
        "llama3.2:latest",
        "anthropic.claude-3-5-sonnet-20240620-v1:0",
        "meta-llama/Llama-3.1-8B-Instruct",
    ],
)
def test_safe_model_label_preserves_dynamic_model_names(safe_model):
    assert safe_model_label(safe_model) == safe_model


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
        "service_id": "",
        "service_name": "",
        "workload_id": "",
        "workload_label": "",
        "stage": "answer",
        "model": "",
        "message": "missing_config",
        "support_id": "",
    }

    legacy = ModelError(
        service="private_service",
        stage="private_diagnostic",
        model="https://10.0.0.8/v1?api_key=sk-private-secret",
        message="RuntimeError: private response",
    )
    assert legacy.model_dump() == {
        "service_id": "",
        "service_name": "",
        "workload_id": "",
        "workload_label": "",
        "stage": "model_call",
        "model": "",
        "message": "upstream_error",
        "support_id": "",
    }


@pytest.mark.parametrize(
    ("stage", "workload_id"),
    [
        ("rerank", "rerank"),
        ("embed", "retrieval_query_embedding"),
        ("chunk_ann_query", "retrieval_query_embedding"),
        ("rewrite", "query_rewrite"),
        ("report_section", "report_section"),
        ("private_diagnostic", "ask_answer"),
    ],
)
def test_model_error_uses_explicit_system_service_and_workload_metadata(
    stage, workload_id
):
    error = ModelError(
        service_id="primary-chat",
        workload_id=workload_id,
        stage=stage,
        message="RuntimeError: private response",
        support_id="mdl-support-safe",
    )

    assert error.service_id == "primary-chat"
    assert error.workload_id == workload_id
    assert error.support_id == "mdl-support-safe"
    assert error.stage == safe_model_error_stage(stage)


def test_explicit_system_service_metadata_wins_over_ignored_legacy_service():
    error = ModelError(
        service="reasoning_llm",
        service_id="reasoning-chat",
        workload_id="reasoning_agent",
        stage="rerank",
        message="RuntimeError: private response",
        support_id="mdl-reasoning-safe",
    )

    assert error.service_id == "reasoning-chat"
    assert error.workload_id == "reasoning_agent"
    assert error.support_id == "mdl-reasoning-safe"
