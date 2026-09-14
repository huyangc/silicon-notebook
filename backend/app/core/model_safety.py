"""Fail-closed labels and codes for client-visible model failure metadata."""
from __future__ import annotations

import re


MODEL_ERROR_UPSTREAM = "upstream_error"
MODEL_ERROR_MISSING_CONFIG = "missing_config"

_MODEL_ERROR_CODES = frozenset({
    MODEL_ERROR_UPSTREAM,
    MODEL_ERROR_MISSING_CONFIG,
    "model_queue_full",
    "model_queue_timeout",
    "model_service_unavailable",
    "provider_auth",
    "provider_rate_limited",
    "provider_unavailable",
    "provider_error",
    "malformed_response",
    "model_not_configured",
    # Deployment-shaped rejections the provider already classifies
    # (``model_provider._stable_error_code``). They used to fall through
    # to ``upstream_error`` here, so a wrong model name or an endpoint that
    # speaks a different protocol rendered as a generic "connection failed".
    "unknown_model",
    "model_not_found",
    "model_rejected",
    "protocol_mismatch",
    "unsupported_protocol",
    "capability_mismatch",
    "unsupported_capability",
})
# Why a ``malformed_response`` was rejected: the JSON-contract boundary's
# ``ModelJsonRepairError.reason`` vocabulary (``core.model_json``), plus the
# two consumer-side verdicts that reuse the same code. Closed set: the client
# renders each value from a fixed label table and anything else collapses to
# "" (unknown), so a future reason can never leak raw diagnostics.
_MODEL_ERROR_DETAILS = frozenset({
    # ``parse_model_json_object`` / repair
    "empty",
    "invalid_json",
    "non_object",
    "incomplete_object",
    "unsupported_syntax",
    "repair_failed",
    "string_changed",
    "serialization_failed",
    "non_finite_number",
    "non_string_key",
    "non_json_value",
    # ``validate_model_json_shape`` against the schema example
    "missing_expected_key",
    "invalid_type",
    "invalid_boolean",
    "invalid_enum",
    "unknown_key",
    # provider-side verdicts that are not parse reasons
    "repairable_shadow",
    "invalid_rerank_rows",
    # answer synthesis: JSON was well-formed but ``answer`` stayed empty on
    # both attempts (ask_service._answer_with_retry)
    "empty_answer",
})
# Provider ``finish_reason`` values worth telling the user about. Only the
# OpenAI-compatible vocabulary; anything else is treated as unknown.
_MODEL_FINISH_REASONS = frozenset({
    "stop",
    "length",
    "content_filter",
    "tool_calls",
    "function_call",
})
_MODEL_SERVICES = frozenset({
    "llm",
    "reasoning_llm",
    "rewrite_llm",
    "kg_llm",
    "rerank",
    "embedding",
})
_MODEL_ERROR_STAGES = frozenset({
    "answer",
    "chunk_ann_delta",
    "chunk_ann_query",
    "chunk_fts",
    "chunk_keyword_union",
    "embed",
    "kg_obj_ann",
    "kg_obj_delta",
    "knowhow_embed",
    "knowhow_complete",
    "knowhow_optimize",
    "ppr_fact_rerank",
    "relation_ann_delta",
    "relation_ann_query",
    "report_section",
    "rerank",
    "rewrite",
    "scale_ann_open_chunk",
    "scale_ann_open_kg",
    "scale_ann_open_relation",
    "scale_ppr_ann",
    "scale_ppr_xbridge_query",
    "tier2_bridge_ann_query",
})
_UNSAFE_MODEL_MARKERS = frozenset({
    "apikey", "authorization", "bearer", "secret", "token", "password",
    "credential", "provider", "diagnostic", "error", "exception", "traceback",
    "runtimeerror", "upstream", "unauthorized", "forbidden", "failure", "failed",
})
_URL_SCHEME_RE = re.compile(r"(?i)[a-z][a-z0-9+.-]*://")
_IPV4_RE = re.compile(
    r"(?<!\d)(?:25[0-5]|2[0-4]\d|1?\d?\d)"
    r"(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?!\d)"
)
_CREDENTIAL_TOKEN_RE = re.compile(r"(?i)(?:sk|rk|pk|ak)-[A-Za-z0-9_-]{8,}")
_VENDOR_KEY_TOKEN_RE = re.compile(r"(?i)(?:aiza|ghp_|hf_|xox)[A-Za-z0-9_-]{8,}")
_HOST_PATH_RE = re.compile(
    r"(?:^|[/@])(?:[^/.\s@:]+\.)+[^/.\s@:]+\.?(?::\d{1,5})?/"
)
_BASIC_AUTH_RE = re.compile(r"(?:^|/)[^:@/\s]+:[^@/\s]+@")
_AWS_ACCESS_ID_RE = re.compile(
    r"(?i)(?:AKIA|ASIA|AIDA|AROA|AIPA|ANPA|ANVA|ASCA)[A-Z0-9]{16}"
)
_STRIPE_KEY_RE = re.compile(r"(?i)(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{8,}")


def safe_model_label(value: object) -> str:
    """Return a bounded model identifier without reflecting config diagnostics."""
    if not isinstance(value, str):
        return ""
    model = value.strip()
    if not model or len(model) > 128 or not model[0].isalnum():
        return ""
    lowered = model.lower()
    compact = "".join(char for char in lowered if char.isalnum())
    if (
        _URL_SCHEME_RE.search(model)
        or _IPV4_RE.search(model)
        or _CREDENTIAL_TOKEN_RE.search(model)
        or _VENDOR_KEY_TOKEN_RE.search(model)
        or _HOST_PATH_RE.search(model)
        or _BASIC_AUTH_RE.search(model)
        or _AWS_ACCESS_ID_RE.search(model)
        or _STRIPE_KEY_RE.search(model)
        or "localhost" in lowered
        or "[" in model
        or "]" in model
        or model.count(":") > 1
        or "//" in model
        or ".." in model
        or any(marker in compact for marker in _UNSAFE_MODEL_MARKERS)
        or _has_endpoint_port(model)
        or _is_bare_hostname(model)
    ):
        return ""
    if any(not (char.isalnum() or char in "._/@+:-") for char in model):
        return ""
    if ":" in model:
        namespace, tag = model.split(":")
        if not namespace or not tag:
            return ""
    if model.endswith((".", "/", ":")):
        return ""
    return model


def infer_model_error_service(stage: object) -> str:
    """Map pre-sanitized legacy stages to the service role they historically implied."""
    value = stage.lower() if isinstance(stage, str) else ""
    if "rerank" in value:
        return "rerank"
    if "embed" in value or "ann" in value:
        return "embedding"
    if "rewrite" in value:
        return "rewrite_llm"
    if value.startswith("kg_"):
        return "kg_llm"
    return "llm"


def safe_model_error_service(value: object) -> str:
    return value if isinstance(value, str) and value in _MODEL_SERVICES else "llm"


def safe_model_error_stage(value: object) -> str:
    return value if isinstance(value, str) and value in _MODEL_ERROR_STAGES else "model_call"


def safe_model_error_code(value: object) -> str:
    return (
        value
        if isinstance(value, str) and value in _MODEL_ERROR_CODES
        else MODEL_ERROR_UPSTREAM
    )


def safe_model_error_detail(value: object) -> str:
    """Closed-set reason behind a ``malformed_response``; unknown → ""."""
    return value if isinstance(value, str) and value in _MODEL_ERROR_DETAILS else ""


def safe_model_finish_reason(value: object) -> str:
    """Provider finish_reason from the closed OpenAI vocabulary; unknown → ""."""
    return value if isinstance(value, str) and value in _MODEL_FINISH_REASONS else ""


_SAFE_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_SAFE_SUPPORT_ID_RE = re.compile(r"mdl-[A-Za-z0-9_-]{1,80}\Z")


def safe_model_metadata_id(value: object) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    return candidate if _SAFE_ID_RE.fullmatch(candidate) else ""


def safe_model_support_id(value: object) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    return candidate if _SAFE_SUPPORT_ID_RE.fullmatch(candidate) else ""


def safe_model_display_name(value: object) -> str:
    """Allow bounded operator labels while rejecting endpoint/secret shapes."""
    if not isinstance(value, str):
        return ""
    candidate = " ".join(value.strip().split())
    if not candidate or len(candidate) > 80:
        return ""
    lowered = candidate.lower()
    compact = "".join(char for char in lowered if char.isalnum())
    if (
        _URL_SCHEME_RE.search(candidate)
        or _IPV4_RE.search(candidate)
        or _CREDENTIAL_TOKEN_RE.search(candidate)
        or _VENDOR_KEY_TOKEN_RE.search(candidate)
        or _AWS_ACCESS_ID_RE.search(candidate)
        or _STRIPE_KEY_RE.search(candidate)
        or "localhost" in lowered
        or any(marker in compact for marker in _UNSAFE_MODEL_MARKERS)
        or any(char in candidate for char in "{}[]<>\\\n\r\t")
    ):
        return ""
    return candidate


def _has_endpoint_port(model: str) -> bool:
    """Treat every valid numeric port as endpoint-like, never as a display tag."""
    if ":" not in model:
        return False
    _, tag = model.rsplit(":", 1)
    if not tag.isdecimal():
        return False
    port = int(tag)
    return 1 <= port <= 65535


def _is_bare_hostname(model: str) -> bool:
    """Reject endpoint-shaped FQDNs while retaining dotted model/tag identifiers."""
    if any(char in model for char in "/@:"):
        return False
    labels = model.rstrip(".").split(".")
    if len(labels) < 2 or any(not label for label in labels):
        return False
    suffix = labels[-1].lower()
    if not (
        (len(suffix) >= 2 and suffix.isalpha())
        or (
            suffix.startswith("xn--")
            and len(suffix) > 4
            and all(char.isascii() and (char.isalnum() or char == "-") for char in suffix)
        )
    ):
        return False
    return all(
        all(char.isalnum() or char in "-_" for char in label)
        for label in labels
    )
