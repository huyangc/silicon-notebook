from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.core.llm import OpenAICompatibleClient
from app.core.model_safety import (
    MODEL_ERROR_MISSING_CONFIG,
    MODEL_ERROR_UPSTREAM,
    infer_model_error_service,
    safe_model_error_service,
    safe_model_error_stage,
    safe_model_label,
)
from app.repositories.sqlite.identity_store import IdentityStore
from app.services.model_concurrency import (
    LimitedJsonChatClient,
    current_model_concurrency,
)
from app.services.model_config import (
    ModelNotConfiguredError,
    ResolvedModelConfig,
    bind_model_status_identity,
    model_config_fingerprint,
    resolve_effective_config,
    system_model_settings,
)
from app.services.model_status import ModelStatusService
from app.services.rerank_client import RerankClient


class _UnconfiguredLLMClient:
    configured = False
    base_url = ""
    api_key = ""
    model = ""

    def chat_json(self, *args, **kwargs):
        raise ModelNotConfiguredError("请先在设置中配置你的模型服务")


_UNCONFIGURED_LLM = _UnconfiguredLLMClient()


class RuntimeModelProvider:
    """Resolve model clients dynamically from the current request identity."""

    def __init__(
        self,
        identity: IdentityStore,
        settings: Settings,
        event_log: Any,
        ask_context: Any,
    ) -> None:
        self.identity = identity
        self.settings = settings
        self.event_log = event_log
        self.ask_context = ask_context
        self.model_config_cache = identity.model_config_cache

        system_configs = system_model_settings(settings)

        def bind_system(client: Any, role: str) -> Any:
            return bind_model_status_identity(
                client,
                resolve_effective_config({}, role, "fallback", system_configs),
            )

        self._system_llm_client = bind_system(OpenAICompatibleClient(settings), "llm")
        self._user_llm_clients: dict[str, OpenAICompatibleClient] = {}
        self._reasoning_llm_client = (
            bind_system(OpenAICompatibleClient(
                settings,
                base_url=settings.reasoning_llm_base_url,
                api_key=settings.reasoning_llm_api_key,
                model=settings.reasoning_llm_model,
            ), "reasoning_llm")
            if settings.reasoning_llm_configured
            else None
        )
        self._rewrite_llm_client = (
            bind_system(OpenAICompatibleClient(
                settings,
                base_url=settings.rewrite_llm_base_url
                or settings.openai_compat_base_url,
                api_key=settings.rewrite_llm_api_key
                or settings.openai_compat_api_key,
                model=settings.rewrite_llm_model,
            ), "rewrite_llm")
            if settings.rewrite_llm_configured
            else None
        )
        self._kg_llm_client = (
            bind_system(OpenAICompatibleClient(
                settings,
                base_url=settings.kg_llm_base_url,
                api_key=settings.kg_llm_api_key,
                model=settings.kg_llm_model,
            ), "kg_llm")
            if settings.kg_llm_configured
            else None
        )
        self._system_rerank_client = bind_system(RerankClient(settings), "rerank")
        self._user_rerank_clients: dict[str, RerankClient] = {}

        if settings.reasoning_llm_partially_configured:
            self.event_log.logger.warning(
                "REASONING_LLM_* 仅部分配置(base_url/api_key/model 需全填)，"
                "推理搜索将回退到全局 OPENAI_COMPAT_* 模型。"
            )

    def _system_llm_for(self, role: str):
        if role == "reasoning_llm":
            return self._reasoning_llm_client or self._system_llm_client
        if role == "rewrite_llm":
            return self._rewrite_llm_client or self._system_llm_client
        if role == "kg_llm":
            return self._kg_llm_client or self._system_llm_client
        return self._system_llm_client

    def _user_llm_cached(self, cfg: ResolvedModelConfig):
        fingerprint = model_config_fingerprint(cfg)
        client = self._user_llm_clients.get(fingerprint)
        if client is None:
            client = bind_model_status_identity(OpenAICompatibleClient(
                self.settings,
                base_url=cfg.base_url,
                api_key=cfg.api_key,
                model=cfg.model,
            ), cfg)
            self._user_llm_clients[fingerprint] = client
        return client

    def _limit_json_client(self, client: Any) -> Any:
        state = current_model_concurrency()
        if state is None:
            return client
        return LimitedJsonChatClient(client, state.llm)

    def _llm_for_role(self, role: str):
        cfg = self.identity.resolve_model_config(self.identity.current_user(), role)
        if cfg.source == "user":
            raw = self._user_llm_cached(cfg)
        elif cfg.source == "none":
            raw = _UNCONFIGURED_LLM
        else:
            raw = self._system_llm_for(role)
        return self._limit_json_client(raw)

    def primary_unconfigured(self) -> bool:
        """Whether the current user has no configured primary LLM."""
        return (
            self.identity.resolve_model_config(
                self.identity.current_user(), "llm"
            ).source
            == "none"
        )

    @property
    def llm_client(self):
        return self._llm_for_role("llm")

    @llm_client.setter
    def llm_client(self, client) -> None:
        self._system_llm_client = client

    @property
    def reasoning_llm_client(self):
        return self._llm_for_role("reasoning_llm")

    @property
    def rewrite_llm_client(self):
        return self._llm_for_role("rewrite_llm")

    @property
    def kg_llm_client(self):
        return self._llm_for_role("kg_llm")

    @property
    def rerank_client(self):
        cfg = self.identity.resolve_model_config(
            self.identity.current_user(), "rerank"
        )
        if cfg.source == "user":
            fingerprint = model_config_fingerprint(cfg)
            client = self._user_rerank_clients.get(fingerprint)
            if client is None:
                client = bind_model_status_identity(RerankClient(
                    self.settings,
                    model=cfg.model,
                    base_url=cfg.base_url,
                    api_key=cfg.api_key,
                    api_style=cfg.api_style,
                ), cfg)
                self._user_rerank_clients[fingerprint] = client
            return client
        if cfg.source == "none":
            return RerankClient(
                self.settings, model="", base_url="", api_key=""
            )
        return self._system_rerank_client

    @rerank_client.setter
    def rerank_client(self, client) -> None:
        self._system_rerank_client = client

    def note_model_error(
        self,
        stage: str,
        model: str,
        error: Exception,
        service: str = "",
        *,
        provider_failure: bool = False,
        failed_fingerprint: str = "",
    ) -> None:
        message = f"{type(error).__name__}: {error}"
        self.event_log.emit(
            {
                "kind": "model_error",
                "stage": stage,
                "model": model or "",
                "error": message[:300],
                "status": "error",
            }
        )
        sink = self.ask_context._ASK_MODEL_ERRORS.get()
        if sink is not None and provider_failure:
            service = safe_model_error_service(service or infer_model_error_service(stage))
            error_code = (
                MODEL_ERROR_MISSING_CONFIG
                if isinstance(error, ModelNotConfiguredError)
                else MODEL_ERROR_UPSTREAM
            )
            sink.append(
                {
                    "service": service,
                    "stage": safe_model_error_stage(stage),
                    "model": safe_model_label(model),
                    "message": error_code,
                }
            )
            if failed_fingerprint:
                try:
                    ModelStatusService(
                        self.identity, self.settings
                    ).record_observed_failure(
                        self.identity.current_user(), service, failed_fingerprint
                    )
                except Exception:
                    self.event_log.logger.exception(
                        "failed to persist observed model failure for %s", service
                    )
