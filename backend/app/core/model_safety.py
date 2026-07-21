"""Fail-closed labels and codes for client-visible model failure metadata."""
from __future__ import annotations

import re


MODEL_ERROR_UPSTREAM = "upstream_error"
MODEL_ERROR_MISSING_CONFIG = "missing_config"

_MODEL_ERROR_CODES = frozenset({
    MODEL_ERROR_UPSTREAM,
    MODEL_ERROR_MISSING_CONFIG,
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
