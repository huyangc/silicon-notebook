"""Validated, process-owned model-service definitions and workload catalog."""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import math
import os
from pathlib import Path
import re
from types import MappingProxyType
import tomllib
from typing import Literal, Mapping
from urllib.parse import urlsplit

from dotenv import dotenv_values

from app.core.config import Settings, _ROOT_DIR
from app.core.model_safety import safe_model_label


ModelKind = Literal["chat", "embedding", "rerank"]
ModelPriorityName = Literal["interactive", "report", "background"]
ThinkingMode = Literal["enabled", "disabled", "provider_default"]
ModelAnalysisArea = Literal[
    "ask", "report", "source", "knowledge", "memory", "knowhow", "retrieval"
]


@dataclass(frozen=True)
class WorkloadSpec:
    id: str
    kind: ModelKind
    default_priority: ModelPriorityName
    display_label: str
    default_thinking_mode: ThinkingMode = "provider_default"
    analysis_area: ModelAnalysisArea = "retrieval"


@dataclass(frozen=True)
class ModelServiceDefinition:
    id: str
    display_name: str
    kind: ModelKind
    protocol: str
    base_url: str
    model: str
    api_key_env: str
    api_key: str = field(repr=False)
    max_concurrency: int
    fingerprint: str
    # Optional service-owned sampling override.  ``None`` preserves the
    # per-call value; a number forces every request sent to this physical chat
    # service to use the configured value.
    top_p: float | None = None


_WORKLOAD_LABELS = MappingProxyType({
    "ask_answer": "问答回答",
    "plugin_engine": "扩展问答引擎",
    "reasoning_agent": "逐步推理",
    "query_rewrite": "查询改写",
    "evidence_refine": "证据筛选",
    "report_outline": "报告提纲",
    "report_sufficiency": "报告充分性判断",
    "report_section": "报告章节生成",
    "report_summary": "报告摘要",
    "source_summary": "来源摘要",
    "notebook_metadata": "笔记本信息提取",
    "paper_metadata": "论文元数据提取",
    "chunk_question_generation": "分块问题生成",
    "kg_extract": "知识抽取",
    "kg_refine": "知识细化",
    "kg_glean": "知识补充抽取",
    "kg_merge_review": "知识合并审核",
    "kg_concept_description": "概念描述生成",
    "kg_community_summary": "知识社区摘要",
    "kg_conflict_review": "知识冲突审核",
    "schema_induction": "知识类型归纳",
    "memory_preview": "记忆预览",
    "knowhow_optimize": "经验表述优化",
    "knowhow_reformat": "经验格式整理",
    "knowhow_complete": "经验空列补全",
    "agent_profile_consolidate": "库理解整理",
    "retrieval_experience_distill": "检索打法总结",
    "retrieval_query_embedding": "检索查询向量",
    "source_element_embedding": "来源元素向量",
    "chunk_embedding": "来源分块向量",
    "knowledge_object_embedding": "知识对象向量",
    "relation_embedding": "知识关系向量",
    "memory_embedding": "记忆向量",
    "knowhow_embedding": "经验表格向量",
    "retrieval_rerank": "检索结果重排",
})

_WORKLOAD_ANALYSIS_AREAS: Mapping[str, ModelAnalysisArea] = MappingProxyType({
    "ask_answer": "ask",
    "plugin_engine": "ask",
    "reasoning_agent": "ask",
    "query_rewrite": "ask",
    "evidence_refine": "ask",
    "report_outline": "report",
    "report_sufficiency": "report",
    "report_section": "report",
    "report_summary": "report",
    "source_summary": "source",
    "notebook_metadata": "source",
    "paper_metadata": "source",
    "chunk_question_generation": "source",
    "kg_extract": "knowledge",
    "kg_refine": "knowledge",
    "kg_glean": "knowledge",
    "kg_merge_review": "knowledge",
    "kg_concept_description": "knowledge",
    "kg_community_summary": "knowledge",
    "kg_conflict_review": "knowledge",
    "schema_induction": "knowledge",
    "memory_preview": "memory",
    "agent_profile_consolidate": "memory",
    "retrieval_experience_distill": "retrieval",
    "knowhow_optimize": "knowhow",
    "knowhow_reformat": "knowhow",
    "knowhow_complete": "knowhow",
})

# Every chat workload makes an explicit product-level choice.  The small set
# below owns multi-step planning/judgement; all other structured extraction,
# classification, rewriting, summarisation, and prose-rendering calls default
# to non-thinking.  Keeping this exhaustive means a newly added chat workload
# cannot silently inherit a provider's potentially expensive reasoning default.
_CHAT_THINKING_DEFAULTS: Mapping[
    str, ThinkingMode
] = MappingProxyType({
    # One final synthesis call has bounded fan-out and directly determines the
    # answer users read.  Keep reasoning here even when retrieval itself was
    # deterministic or already used the reasoning agent.
    "ask_answer": "enabled",
    "plugin_engine": "disabled",
    "reasoning_agent": "enabled",
    "query_rewrite": "disabled",
    "evidence_refine": "disabled",
    "report_outline": "enabled",
    "report_sufficiency": "enabled",
    "report_section": "disabled",
    # Report sections already inherit a report-wide blueprint and fan out by
    # section.  The final editor audits but cannot rewrite those bodies, so
    # both stages remain non-thinking; reasoning is spent in outline and
    # sufficiency planning instead.
    "report_summary": "disabled",
    "source_summary": "disabled",
    "notebook_metadata": "disabled",
    "paper_metadata": "disabled",
    "chunk_question_generation": "disabled",
    "kg_extract": "disabled",
    "kg_refine": "disabled",
    "kg_glean": "disabled",
    "kg_merge_review": "disabled",
    "kg_concept_description": "disabled",
    "kg_community_summary": "disabled",
    "kg_conflict_review": "disabled",
    "schema_induction": "enabled",
    "memory_preview": "disabled",
    "knowhow_optimize": "disabled",
    "knowhow_reformat": "disabled",
    "knowhow_complete": "disabled",
    # These are bounded, low-frequency synthesis calls whose persisted result
    # affects later retrieval quality; reasoning has durable leverage here.
    "agent_profile_consolidate": "enabled",
    "retrieval_experience_distill": "enabled",
})


def workload_map(
    *,
    chat: Mapping[str, ModelPriorityName],
    embedding: Mapping[str, ModelPriorityName],
    rerank: Mapping[str, ModelPriorityName],
) -> Mapping[str, WorkloadSpec]:
    """Construct the immutable catalog from the only approved workload data."""
    if set(chat) != set(_CHAT_THINKING_DEFAULTS):
        raise ValueError(
            "chat workload catalog does not match thinking defaults"
        )
    if set(chat) != set(_WORKLOAD_ANALYSIS_AREAS):
        raise ValueError(
            "chat workload catalog does not match analysis-area registry"
        )
    result: dict[str, WorkloadSpec] = {}
    for kind, items in (("chat", chat), ("embedding", embedding), ("rerank", rerank)):
        for workload_id, priority in items.items():
            if workload_id in result or priority not in {"interactive", "report", "background"}:
                raise ValueError(f"invalid workload definition: {workload_id}")
            try:
                label = _WORKLOAD_LABELS[workload_id]
            except KeyError as exc:
                raise ValueError(f"missing workload display label: {workload_id}") from exc
            result[workload_id] = WorkloadSpec(
                workload_id,
                kind,  # type: ignore[arg-type]
                priority,
                label,
                _CHAT_THINKING_DEFAULTS.get(
                    workload_id, "provider_default"
                ),
                _WORKLOAD_ANALYSIS_AREAS.get(workload_id, "retrieval"),
            )
    if set(result) != set(_WORKLOAD_LABELS):
        raise ValueError("workload catalog does not match its display-label registry")
    return MappingProxyType(result)


WORKLOADS = workload_map(
    chat={
        "ask_answer": "interactive", "plugin_engine": "interactive",
        "reasoning_agent": "interactive",
        "query_rewrite": "interactive", "evidence_refine": "interactive",
        "report_outline": "report",
        "report_sufficiency": "report", "report_section": "report",
        "report_summary": "report", "source_summary": "background",
        "notebook_metadata": "background", "paper_metadata": "background",
        "chunk_question_generation": "background",
        "kg_extract": "background", "kg_refine": "background",
        "kg_glean": "background", "kg_merge_review": "background",
        "kg_concept_description": "background",
        "kg_community_summary": "background",
        "kg_conflict_review": "background", "schema_induction": "interactive",
        "memory_preview": "interactive", "knowhow_optimize": "interactive",
        "knowhow_reformat": "interactive", "knowhow_complete": "interactive",
        # Agentic Memory P1: one bounded call per consolidation run, fired by a
        # deterministic threshold and never by a user waiting on a response —
        # "background", the same priority class as the other write-path jobs.
        "agent_profile_consolidate": "background",
        # Agentic Memory P2: one bounded call per distillation batch,
        # fired by a deterministic global threshold and never by a user
        # waiting on a response — "background", like its P1 sibling.
        "retrieval_experience_distill": "background",
    },
    embedding={
        "retrieval_query_embedding": "interactive",
        "source_element_embedding": "background", "chunk_embedding": "background",
        "knowledge_object_embedding": "background", "relation_embedding": "background",
        "memory_embedding": "interactive", "knowhow_embedding": "background",
    },
    rerank={"retrieval_rerank": "interactive"},
)


# Workload ids that once existed and may still appear in deployed
# MODEL_SERVICES_CONFIG files ([bindings]/[thinking]) generated from earlier
# checked-in examples or the legacy migration script.  They are ACCEPTED and
# DROPPED during load — the same upgrade contract as ask_modes._RETIRED_MODES:
# retiring a capability must never brick startup for a configuration that was
# valid before the upgrade.  Entries here carry no behavior; nothing reads a
# retired binding after load.
RETIRED_WORKLOADS = frozenset({
    # graph ask mode retired (this branch); its chain-verification workload
    # went with it.  verify_chain_edges survives as an unwired library helper.
    "graph_chain_verify",
})

_SERVICE_ID_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_REQUIRED_SERVICE_KEYS = frozenset({
    "display_name", "kind", "protocol", "base_url", "model", "api_key_env",
    "max_concurrency",
})
_OPTIONAL_SERVICE_KEYS = frozenset({"top_p"})
_SERVICE_KEYS = _REQUIRED_SERVICE_KEYS | _OPTIONAL_SERVICE_KEYS
_TOP_LEVEL_KEYS = frozenset({"services", "bindings", "thinking"})
_THINKING_MODES = frozenset({"enabled", "disabled", "provider_default"})
_PROTOCOLS: Mapping[str, frozenset[str]] = MappingProxyType({
    "chat": frozenset({"openai"}),
    "embedding": frozenset({"openai", "dashscope"}),
    "rerank": frozenset({"openai", "dashscope"}),
})
_LEGACY_MODEL_ACTIVATION_VARS = (
    "OPENAI_COMPAT_BASE_URL",
    "OPENAI_COMPAT_API_KEY",
    "OPENAI_COMPAT_MODEL",
    "REASONING_LLM_BASE_URL",
    "REASONING_LLM_API_KEY",
    "REASONING_LLM_MODEL",
    "REWRITE_LLM_BASE_URL",
    "REWRITE_LLM_API_KEY",
    "REWRITE_LLM_MODEL",
    "KG_LLM_BASE_URL",
    "KG_LLM_API_KEY",
    "KG_LLM_MODEL",
    "EMBED_PROVIDER",
    "EMBED_BASE_URL",
    "EMBED_API_KEY",
    "EMBED_MODEL",
    "RERANK_BASE_URL",
    "RERANK_API_KEY",
    "RERANK_MODEL",
    "RERANK_API_STYLE",
)


class SystemModelServiceRegistry:
    """Immutable, validated mapping of workloads to physical model services."""

    def __init__(
        self,
        services: Mapping[str, ModelServiceDefinition],
        bindings: Mapping[str, str],
        thinking_modes: Mapping[str, ThinkingMode] | None = None,
    ) -> None:
        self._services = MappingProxyType(dict(services))
        self._bindings = MappingProxyType(dict(bindings))
        self._thinking_modes = MappingProxyType(
            dict(thinking_modes or {})
        )
        grouped: dict[str, list[WorkloadSpec]] = {service_id: [] for service_id in services}
        for workload_id, service_id in bindings.items():
            grouped[service_id].append(WORKLOADS[workload_id])
        self._workloads_by_service = MappingProxyType({
            service_id: tuple(workloads)
            for service_id, workloads in grouped.items()
        })

    @classmethod
    def load(
        cls,
        settings: Settings,
        environ: Mapping[str, str] | None = None,
    ) -> "SystemModelServiceRegistry":
        effective_env = _effective_environ(settings, environ)
        config_path = (settings.model_services_config or "").strip()
        if not config_path:
            _reject_legacy_model_configuration(effective_env)
            return cls({}, {})

        path = Path(config_path)
        if not path.is_absolute():
            path = _ROOT_DIR / path
        try:
            with path.open("rb") as handle:
                parsed = tomllib.load(handle)
        except OSError as exc:
            raise ValueError(f"unable to read MODEL_SERVICES_CONFIG: {path}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ValueError("MODEL_SERVICES_CONFIG contains invalid TOML") from exc

        _reject_unknown_keys(parsed, _TOP_LEVEL_KEYS, "MODEL_SERVICES_CONFIG")
        raw_services = parsed.get("services", {})
        raw_bindings = parsed.get("bindings", {})
        raw_thinking = parsed.get("thinking", {})
        if not all(
            isinstance(value, dict)
            for value in (raw_services, raw_bindings, raw_thinking)
        ):
            raise ValueError(
                "MODEL_SERVICES_CONFIG services, bindings, and thinking "
                "must be tables"
            )

        services: dict[str, ModelServiceDefinition] = {}
        physical_definitions: set[tuple[str, str, str, str, str]] = set()
        for service_id, raw_definition in raw_services.items():
            definition = _parse_service(service_id, raw_definition, effective_env)
            physical = (
                definition.kind,
                definition.protocol,
                definition.base_url,
                definition.model,
                definition.api_key,
            )
            if physical in physical_definitions:
                raise ValueError(
                    f"duplicate physical model service definition: {definition.id}"
                )
            physical_definitions.add(physical)
            services[definition.id] = definition

        bindings: dict[str, str] = {}
        for workload_id, service_id in raw_bindings.items():
            if workload_id in RETIRED_WORKLOADS:
                # Dropped BEFORE every other check: the referenced service may
                # itself have been removed alongside the retired workload, and
                # a retired entry must not fail any validation it used to pass.
                continue
            if workload_id not in WORKLOADS:
                raise ValueError(f"unknown model workload binding: {workload_id}")
            if not isinstance(service_id, str) or service_id not in services:
                raise ValueError(
                    f"model workload {workload_id} references unknown service {service_id}"
                )
            service = services[service_id]
            if service.kind != WORKLOADS[workload_id].kind:
                raise ValueError(f"model workload {workload_id} has a mismatched service kind")
            bindings[workload_id] = service_id

        thinking_modes: dict[str, ThinkingMode] = {}
        for workload_id, raw_mode in raw_thinking.items():
            if workload_id in RETIRED_WORKLOADS:
                continue
            workload = WORKLOADS.get(workload_id)
            if workload is None:
                raise ValueError(f"unknown model thinking workload: {workload_id}")
            if workload.kind != "chat":
                raise ValueError(
                    f"model thinking mode is only valid for chat workload {workload_id}"
                )
            if not isinstance(raw_mode, str) or raw_mode not in _THINKING_MODES:
                raise ValueError(
                    f"model workload {workload_id} has an invalid thinking mode"
                )
            thinking_modes[workload_id] = raw_mode  # type: ignore[assignment]
        return cls(services, bindings, thinking_modes)

    def service(self, service_id: str) -> ModelServiceDefinition:
        return self._services[service_id]

    def services(self) -> tuple[ModelServiceDefinition, ...]:
        return tuple(self._services.values())

    def service_for(self, workload_id: str) -> ModelServiceDefinition | None:
        service_id = self._bindings.get(workload_id)
        return self._services[service_id] if service_id else None

    def workload(self, workload_id: str) -> WorkloadSpec:
        return WORKLOADS[workload_id]

    def thinking_mode_for(
        self, workload_id: str
    ) -> ThinkingMode:
        workload = WORKLOADS[workload_id]
        return self._thinking_modes.get(
            workload_id, workload.default_thinking_mode
        )

    def workloads_for(self, service_id: str) -> tuple[WorkloadSpec, ...]:
        return self._workloads_by_service.get(service_id, ())


def _effective_environ(
    settings: Settings,
    environ: Mapping[str, str] | None,
) -> Mapping[str, str]:
    values: dict[str, str] = {}
    env_file = settings.model_config.get("env_file")
    env_paths = env_file if isinstance(env_file, (list, tuple)) else (env_file,)
    for candidate in env_paths:
        if not candidate:
            continue
        for key, value in dotenv_values(candidate).items():
            if value is not None:
                values[key] = value
    values.update(os.environ if environ is None else environ)
    return MappingProxyType(values)


def _reject_legacy_model_configuration(environ: Mapping[str, str]) -> None:
    """Fail closed before retired fields can select an implicit legacy client."""
    configured = tuple(
        variable
        for variable in _LEGACY_MODEL_ACTIVATION_VARS
        if (environ.get(variable) or "").strip()
    )
    if configured:
        raise ValueError(
            f"{', '.join(configured)} are retired; configure MODEL_SERVICES_CONFIG instead"
        )


def _parse_service(
    service_id: object,
    raw_definition: object,
    environ: Mapping[str, str],
) -> ModelServiceDefinition:
    if not isinstance(service_id, str) or not _SERVICE_ID_RE.fullmatch(service_id):
        raise ValueError("invalid model service id")
    if not isinstance(raw_definition, dict):
        raise ValueError(f"model service {service_id} must be a TOML table")
    _reject_unknown_keys(raw_definition, _SERVICE_KEYS, f"model service {service_id}")
    missing = _REQUIRED_SERVICE_KEYS.difference(raw_definition)
    if missing:
        raise ValueError(f"model service {service_id} is missing required fields")

    display_name = _required_text(raw_definition["display_name"], "display_name")
    if safe_model_label(display_name) != display_name:
        raise ValueError(f"model service {service_id} has an unsafe display_name")
    kind = _required_text(raw_definition["kind"], "kind")
    protocol = _required_text(raw_definition["protocol"], "protocol").lower()
    if kind not in _PROTOCOLS:
        raise ValueError(f"model service {service_id} has an invalid kind")
    if protocol not in _PROTOCOLS[kind]:
        raise ValueError(f"model service {service_id} has an unsupported protocol")
    base_url = _normalized_url(raw_definition["base_url"])
    model = _required_text(raw_definition["model"], "model")
    if safe_model_label(model) != model:
        raise ValueError(f"model service {service_id} has an unsafe model")
    api_key_env = _required_text(raw_definition["api_key_env"], "api_key_env")
    if not _ENV_NAME_RE.fullmatch(api_key_env):
        raise ValueError(f"model service {service_id} has an invalid api_key_env")
    api_key = (environ.get(api_key_env) or "").strip()
    if not api_key:
        raise ValueError(f"model service {service_id} is missing {api_key_env}")
    max_concurrency = raw_definition["max_concurrency"]
    if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency <= 0:
        raise ValueError(f"model service {service_id} has an invalid max_concurrency")
    top_p = _optional_top_p(raw_definition.get("top_p"), kind, service_id)
    fingerprint = _fingerprint(
        service_id, kind, protocol, base_url, model, api_key,
        str(max_concurrency), "" if top_p is None else repr(top_p),
    )
    return ModelServiceDefinition(
        id=service_id,
        display_name=display_name,
        kind=kind,  # type: ignore[arg-type]
        protocol=protocol,
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        api_key=api_key,
        max_concurrency=max_concurrency,
        fingerprint=fingerprint,
        top_p=top_p,
    )


def _optional_top_p(value: object, kind: str, service_id: str) -> float | None:
    if value is None:
        return None
    if kind != "chat":
        raise ValueError(f"model service {service_id} top_p is only valid for chat services")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"model service {service_id} has an invalid top_p")
    normalized = float(value)
    if not math.isfinite(normalized) or not 0.0 <= normalized <= 1.0:
        raise ValueError(f"model service {service_id} has an invalid top_p")
    return normalized


def _reject_unknown_keys(raw: Mapping[object, object], allowed: frozenset[str], context: str) -> None:
    unknown = sorted(str(key) for key in set(raw).difference(allowed))
    if unknown:
        raise ValueError(f"{context} contains unknown keys: {', '.join(unknown)}")


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not (normalized := value.strip()):
        raise ValueError(f"model service has an invalid {field_name}")
    return normalized


def _normalized_url(value: object) -> str:
    url = _required_text(value, "base_url").rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password:
        raise ValueError("model service has an invalid base_url")
    return url


def _fingerprint(*parts: str) -> str:
    return sha256("\0".join(parts).encode("utf-8")).hexdigest()
