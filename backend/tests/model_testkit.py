"""Explicit workload-level model fakes for repository integration tests."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


class UnconfiguredChatClient:
    configured = False
    model = ""

    def chat_json(self, *args, **kwargs):
        raise RuntimeError("model not configured")


UNCONFIGURED_CHAT_CLIENT = UnconfiguredChatClient()


class UnconfiguredEmbedder:
    configured = False
    dim = 16

    def embed_texts(self, texts):
        return [[0.0] * self.dim for _ in texts]

    def embed_query(self, text):
        return [0.0] * self.dim


class UnconfiguredReranker:
    configured = False

    def rerank(self, query, documents, on_error=None):
        return list(range(len(documents)))


UNCONFIGURED_EMBEDDER = UnconfiguredEmbedder()
UNCONFIGURED_RERANKER = UnconfiguredReranker()


@dataclass
class RecordingModelProvider:
    chat_clients: Mapping[str, Any] = field(default_factory=dict)
    embedding_clients: Mapping[str, Any] = field(default_factory=dict)
    rerank_clients: Mapping[str, Any] = field(default_factory=dict)
    parallelism_by_workload: Mapping[str, int] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    error_calls: list[dict[str, Any]] = field(default_factory=list)
    closed: int = 0

    def chat(self, workload_id: str) -> Any:
        self.calls.append(("chat", workload_id))
        return self.chat_clients.get(workload_id, UNCONFIGURED_CHAT_CLIENT)

    def embedding(self, workload_id: str) -> Any:
        self.calls.append(("embedding", workload_id))
        return self.embedding_clients.get(workload_id, UNCONFIGURED_EMBEDDER)

    def rerank(self, workload_id: str) -> Any:
        self.calls.append(("rerank", workload_id))
        return self.rerank_clients.get(workload_id, UNCONFIGURED_RERANKER)

    @property
    def rerank_client(self) -> Any:
        return self.rerank("retrieval_rerank")

    def configured(self, workload_id: str) -> bool:
        delegates = (
            self.chat_clients,
            self.embedding_clients,
            self.rerank_clients,
        )
        return any(
            workload_id in mapping
            and bool(getattr(mapping[workload_id], "configured", True))
            for mapping in delegates
        )

    def parallelism(self, workload_id: str) -> int:
        return max(1, int(self.parallelism_by_workload.get(workload_id, 1)))

    def note_model_error(
        self, stage: str, error: Exception, *, workload_id: str
    ) -> None:
        self.error_calls.append(
            {"stage": stage, "error": error, "workload_id": workload_id}
        )

    def probe(self, service_id: str, *, actor_id: str, allow_half_open: bool):
        self.calls.append(("probe", service_id))
        raise NotImplementedError("RecordingModelProvider probes must be explicit")

    def scheduler_snapshot(self, service_id: str):
        self.calls.append(("snapshot", service_id))
        raise NotImplementedError("RecordingModelProvider snapshots must be explicit")

    def close(self) -> None:
        self.closed += 1


def bind_chat_client(repo: Any, workload_id: str, client: Any) -> None:
    """Bind one explicit workload on an already-composed test repository.

    Most tests should inject ``RecordingModelProvider`` at construction.  HTTP
    tests use the process repository, so this helper overrides the provider's
    workload methods without restoring the retired repository role setters.
    """
    provider = repo._runtime.models
    if isinstance(provider, RecordingModelProvider):
        provider.chat_clients = {**provider.chat_clients, workload_id: client}
        return

    overrides = getattr(provider, "_test_chat_overrides", None)
    if overrides is None:
        overrides = {}
        provider._test_chat_overrides = overrides
        provider._test_original_chat = provider.chat
        provider._test_original_configured = provider.configured
        provider.chat = lambda requested: (
            overrides[requested]
            if requested in overrides
            else provider._test_original_chat(requested)
        )
        provider.configured = lambda requested: (
            bool(getattr(overrides[requested], "configured", True))
            if requested in overrides
            else provider._test_original_configured(requested)
        )
    overrides[workload_id] = client
