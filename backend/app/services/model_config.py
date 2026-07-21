"""每用户模型服务配置解析。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping


MODEL_SERVICE_ROLES = ("llm", "reasoning_llm", "rewrite_llm", "kg_llm", "rerank")
STATUS_SERVICE_ROLES = (*MODEL_SERVICE_ROLES, "embedding")
LLM_VARIANTS = ("reasoning_llm", "rewrite_llm", "kg_llm")
_OPENAI_RERANK_STYLES = frozenset({"openai", "vllm", "cohere", "compatible"})


class ModelNotConfiguredError(RuntimeError):
    """policy=required 且用户未配置该服务时抛出，经 model_error 通道提示用户。"""


@dataclass(frozen=True)
class ResolvedModelConfig:
    base_url: str
    api_key: str
    model: str
    source: str   # "user" | "system" | "none"
    kind: str = "llm"
    provider: str = ""
    api_style: str = ""

    @property
    def configured(self) -> bool:
        if self.kind == "embedding":
            return self.provider == "dashscope" and all(
                value.strip() for value in (self.base_url, self.api_key, self.model)
            )
        return bool(self.base_url and self.api_key and self.model)


def normalize_embedding_provider(value: object) -> str:
    return str(value or "").strip().lower()


def normalize_rerank_api_style(value: object) -> str:
    normalized = str(value or "dashscope").strip().lower()
    return "openai" if normalized in _OPENAI_RERANK_STYLES else "dashscope"


def _full(svc: dict) -> bool:
    return bool(svc.get("base_url") and svc.get("api_key") and svc.get("model"))


def system_model_settings(settings) -> dict[str, dict[str, str]]:
    primary = {
        "base_url": settings.openai_compat_base_url,
        "api_key": settings.openai_compat_api_key,
        "model": settings.openai_compat_model,
        "source": "system",
        "kind": "llm",
        "provider": "openai_compatible",
    }
    reasoning = (
        {
            "base_url": settings.reasoning_llm_base_url,
            "api_key": settings.reasoning_llm_api_key,
            "model": settings.reasoning_llm_model,
            "source": "system",
            "kind": "llm",
            "provider": "openai_compatible",
        }
        if settings.reasoning_llm_configured else primary
    )
    rewrite = (
        {
            "base_url": settings.rewrite_llm_base_url or settings.openai_compat_base_url,
            "api_key": settings.rewrite_llm_api_key or settings.openai_compat_api_key,
            "model": settings.rewrite_llm_model,
            "source": "system",
            "kind": "llm",
            "provider": "openai_compatible",
        }
        if settings.rewrite_llm_configured else primary
    )
    kg = (
        {
            "base_url": settings.kg_llm_base_url,
            "api_key": settings.kg_llm_api_key,
            "model": settings.kg_llm_model,
            "source": "system",
            "kind": "llm",
            "provider": "openai_compatible",
        }
        if settings.kg_llm_configured else primary
    )
    return {
        "llm": primary,
        "reasoning_llm": reasoning,
        "rewrite_llm": rewrite,
        "kg_llm": kg,
        "rerank": {
            "base_url": settings.rerank_base_url,
            "api_key": settings.rerank_api_key,
            "model": settings.rerank_model,
            "source": "system",
            "kind": "rerank",
            "provider": "http",
            "api_style": normalize_rerank_api_style(settings.rerank_api_style),
        },
        "embedding": {
            "base_url": settings.embed_base_url,
            "api_key": settings.embed_api_key,
            "model": settings.embed_model,
            "source": "system",
            "kind": "embedding",
            "provider": normalize_embedding_provider(settings.embed_provider),
        },
    }


def resolve_effective_config(
    model_settings: dict,
    role: str,
    policy: str,
    system_settings: Mapping[str, Mapping[str, str]] | None = None,
) -> ResolvedModelConfig:
    if role == "embedding":
        svc = dict((system_settings or {}).get(role) or {})
        return ResolvedModelConfig(
            svc.get("base_url", ""),
            svc.get("api_key", ""),
            svc.get("model", ""),
            svc.get("source", "system"),
            "embedding",
            normalize_embedding_provider(svc.get("provider", "")),
        )
    svc = (model_settings or {}).get(role) or {}
    if _full(svc):
        kind = "rerank" if role == "rerank" else "llm"
        system = dict((system_settings or {}).get(role) or {})
        return ResolvedModelConfig(
            svc["base_url"],
            svc["api_key"],
            svc["model"],
            "user",
            kind,
            "http" if role == "rerank" else "openai_compatible",
            normalize_rerank_api_style(system.get("api_style")) if role == "rerank" else "",
        )
    # 第 1 层：变体 LLM 未配 → 回退到用户自己的主 LLM
    if role in LLM_VARIANTS:
        primary = (model_settings or {}).get("llm") or {}
        if _full(primary):
            return ResolvedModelConfig(
                primary["base_url"], primary["api_key"], primary["model"],
                "user", "llm", "openai_compatible",
            )
    # 第 2 层：用户没配 → 按 policy
    if policy == "required":
        return ResolvedModelConfig(
            "", "", "", "none", "rerank" if role == "rerank" else "llm"
        )
    system = dict((system_settings or {}).get(role) or {})
    return ResolvedModelConfig(
        system.get("base_url", ""),
        system.get("api_key", ""),
        system.get("model", ""),
        system.get("source", "system"),
        system.get("kind", "rerank" if role == "rerank" else "llm"),
        str(system.get("provider", "")),
        normalize_rerank_api_style(system.get("api_style")) if role == "rerank" else "",
    )


def model_config_fingerprint(config: ResolvedModelConfig) -> str:
    material = "\0".join(
        (
            config.kind,
            config.source,
            config.provider,
            config.api_style,
            config.base_url,
            config.model,
            config.api_key,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
