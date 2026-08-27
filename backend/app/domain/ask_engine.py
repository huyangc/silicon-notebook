"""Application-facing port for the startup-frozen Ask engine host."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.domain.extensions import CancellationToken


PLUGIN_ENGINE_ERROR_CODES = frozenset({
    "invalid_plugin_engine_result",
    "plugin_engine_cancelled",
    "plugin_engine_citation_limit",
    "plugin_engine_failed",
    "plugin_engine_invalid_cancellation",
    "plugin_engine_invalid_evidence_key",
    "plugin_engine_invalid_kg_request",
    "plugin_engine_invalid_prompt",
    "plugin_engine_invalid_query",
    "plugin_engine_invalid_retrieval_limit",
    "plugin_engine_kg_call_limit",
    "plugin_engine_model_call_limit",
    "plugin_engine_model_failed",
    "plugin_engine_model_unconfigured",
    "plugin_engine_prompt_too_long",
    "plugin_engine_query_too_long",
    "plugin_engine_search_call_limit",
    "plugin_engine_unavailable",
    "plugin_engine_unverified_citation",
})


def safe_plugin_engine_error_code(value: object) -> str:
    """Admit only the finite core-authored vocabulary at a plugin boundary."""

    return (
        value
        if type(value) is str and value in PLUGIN_ENGINE_ERROR_CODES
        else "plugin_engine_failed"
    )


class AskPluginEngineError(RuntimeError):
    """Core-authored, content-free failure safe for durable job state."""

    def __init__(self, code: str) -> None:
        safe_code = safe_plugin_engine_error_code(code)
        super().__init__(safe_code)
        self.code = safe_code


@dataclass(frozen=True, slots=True)
class AskEngineDescriptor:
    """Static, user-facing metadata for one deployment Ask engine."""

    mode_id: str
    label: str
    description: str
    requires_kg: bool


@dataclass(frozen=True, slots=True)
class AskEngineContext:
    """The complete request projection visible to a plugin engine in v1.

    刻意**不带** notebook/actor id(codex #602 R12 P2):合同(docs/product-and-api*.md ask.engine
    条)写明 provider 只收当前问题与四个端口——稳定身份 id 交给插件只会打开跨 run
    关联/自记日志的口子,范围与归属全部由核心在端口构造时预绑定,插件不需要它们。
    """

    question: str
    cancellation: CancellationToken


@dataclass(frozen=True, slots=True)
class EngineEvidence:
    """A bounded excerpt addressed only by a run-local opaque handle."""

    evidence_key: str
    text: str
    source_title: str
    location_label: str
    # "" = source-element hit; one of the four core node types = knowledge
    # object hit. Appended with a default so v1 providers that build the
    # four-positional form stay valid.
    object_type: str = ""


@dataclass(frozen=True, slots=True)
class AskEngineResult:
    answer_markdown: str
    citations: tuple[str, ...]


class AskEnginePortError(RuntimeError):
    """Stable, content-free rejection raised by a core-owned narrow port."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RetrievalAccessPort(Protocol):
    def search(self, query: str, k: int) -> tuple[EngineEvidence, ...]: ...

    def fetch(self, evidence_key: str) -> EngineEvidence | None: ...

    def search_kg(
        self, query: str, k: int, object_types: tuple[str, ...] = ()
    ) -> tuple[EngineEvidence, ...]: ...

    def kg_neighbors(
        self,
        evidence_key: str,
        k: int,
        edge_type: str = "",
        direction: str = "both",
    ) -> tuple[EngineEvidence, ...]: ...

    def kg_overview(self) -> str: ...


class EngineModelPort(Protocol):
    def complete(self, prompt: str) -> str: ...


class EngineTraceSink(Protocol):
    def step(self, label: str, detail: str = "") -> None: ...


class AskEngineProvider(Protocol):
    descriptor: AskEngineDescriptor

    def answer(
        self,
        context: AskEngineContext,
        retrieval: RetrievalAccessPort,
        model: EngineModelPort,
        trace: EngineTraceSink,
    ) -> AskEngineResult: ...


@dataclass(frozen=True, slots=True)
class AskEngineAvailabilityContext:
    contribution_id: str
    mode_id: str


class AskEngineHostPort(Protocol):
    def has_engines(self) -> bool: ...

    def modes(self) -> tuple[Any, ...]: ...

    def mode(self, mode_id: str) -> Any | None: ...

    def registrations(self) -> tuple[Any, ...]: ...

    def is_available(self, mode_id: str) -> bool: ...

    def answer(
        self,
        mode_id: str,
        context: Any,
        retrieval: Any,
        model: Any,
        trace: Any,
        *,
        event_sink: Any | None = None,
    ) -> Any: ...


__all__ = [
    "AskEngineAvailabilityContext",
    "AskEngineContext",
    "AskEngineDescriptor",
    "AskEngineHostPort",
    "AskEnginePortError",
    "AskEngineProvider",
    "AskEngineResult",
    "AskPluginEngineError",
    "EngineEvidence",
    "EngineModelPort",
    "EngineTraceSink",
    "PLUGIN_ENGINE_ERROR_CODES",
    "RetrievalAccessPort",
    "safe_plugin_engine_error_code",
]
