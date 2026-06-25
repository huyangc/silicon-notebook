"""每用户模型服务配置解析。纯函数 resolve_effective_config 便于单测；仓库侧
resolve_model_config 注入用户的 model_settings + 全局 policy。"""
from __future__ import annotations
from dataclasses import dataclass

LLM_VARIANTS = ("reasoning_llm", "rewrite_llm", "kg_llm")


class ModelNotConfiguredError(RuntimeError):
    """policy=required 且用户未配置该服务时抛出，经 model_error 通道提示用户。"""


@dataclass(frozen=True)
class ResolvedModelConfig:
    base_url: str
    api_key: str
    model: str
    source: str   # "user" | "system" | "none"


def _full(svc: dict) -> bool:
    return bool(svc.get("base_url") and svc.get("api_key") and svc.get("model"))


def resolve_effective_config(model_settings: dict, role: str, policy: str) -> ResolvedModelConfig:
    svc = (model_settings or {}).get(role) or {}
    if _full(svc):
        return ResolvedModelConfig(svc["base_url"], svc["api_key"], svc["model"], "user")
    # 第 1 层：变体 LLM 未配 → 回退到用户自己的主 LLM
    if role in LLM_VARIANTS:
        primary = (model_settings or {}).get("llm") or {}
        if _full(primary):
            return ResolvedModelConfig(primary["base_url"], primary["api_key"], primary["model"], "user")
    # 第 2 层：用户没配 → 按 policy
    if policy == "required":
        return ResolvedModelConfig("", "", "", "none")
    return ResolvedModelConfig("", "", "", "system")
