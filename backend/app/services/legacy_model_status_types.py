"""Short-lived types for the legacy per-user status table.

Task 6 removes these persistence interfaces.  This module deliberately owns
no endpoint resolution, provider selection, client construction, or fallback
policy; product model traffic is exclusively system-workload bound.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib


MODEL_SERVICE_ROLES = ("llm", "reasoning_llm", "rewrite_llm", "kg_llm", "rerank")
STATUS_SERVICE_ROLES = (*MODEL_SERVICE_ROLES, "embedding")


@dataclass(frozen=True)
class ResolvedModelConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    source: str = "none"
    kind: str = "llm"
    provider: str = ""
    api_style: str = ""

    @property
    def configured(self) -> bool:
        return False


def unresolved_model_status_config(role: str) -> ResolvedModelConfig:
    kind = "embedding" if role == "embedding" else ("rerank" if role == "rerank" else "llm")
    return ResolvedModelConfig(kind=kind)


def model_config_fingerprint(config: ResolvedModelConfig) -> str:
    material = "\0".join((
        config.kind, config.source, config.provider, config.api_style,
        config.base_url, config.model, config.api_key,
    ))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
