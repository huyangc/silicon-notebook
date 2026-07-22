"""Explicit workload-level model fakes for repository integration tests."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from app.services.model_registry import WORKLOADS


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


def _bind_provider_client(repo: Any, kind: str, workload_id: str, client: Any) -> None:
    if WORKLOADS[workload_id].kind != kind:
        raise ValueError(f"{workload_id} is not a {kind} workload")
    provider = repo._runtime.models
    mapping_name = {
        "chat": "chat_clients",
        "embedding": "embedding_clients",
        "rerank": "rerank_clients",
    }[kind]
    method_name = {
        "chat": "chat",
        "embedding": "embedding",
        "rerank": "rerank",
    }[kind]
    if isinstance(provider, RecordingModelProvider):
        mapping = getattr(provider, mapping_name)
        setattr(provider, mapping_name, {**mapping, workload_id: client})
        return

    overrides_name = f"_test_{kind}_overrides"
    overrides = getattr(provider, overrides_name, None)
    if overrides is None:
        overrides = {}
        setattr(provider, overrides_name, overrides)
        calls: list[str] = []
        setattr(provider, f"_test_{kind}_calls", calls)
        original = getattr(provider, method_name)
        setattr(provider, f"_test_original_{method_name}", original)

        def test_client(requested):
            calls.append(requested)
            return overrides[requested] if requested in overrides else original(requested)

        setattr(provider, method_name, test_client)
        original_configured = provider.configured
        if not hasattr(provider, "_test_original_configured"):
            provider._test_original_configured = original_configured

            def test_configured(requested):
                for candidate_kind in ("chat", "embedding", "rerank"):
                    candidate_overrides = getattr(
                        provider, f"_test_{candidate_kind}_overrides", {}
                    )
                    if requested in candidate_overrides:
                        return bool(
                            getattr(candidate_overrides[requested], "configured", True)
                        )
                return provider._test_original_configured(requested)

            provider.configured = test_configured
    overrides[workload_id] = client


def bind_chat_client(repo: Any, workload_id: str, client: Any) -> None:
    """Bind one explicit workload on an already-composed test repository.

    Most tests should inject ``RecordingModelProvider`` at construction.  HTTP
    tests use the process repository, so this helper overrides the provider's
    workload methods without restoring the retired repository role setters.
    """
    _bind_provider_client(repo, "chat", workload_id, client)


def bind_embedding_client(
    repo: Any,
    client: Any,
    workload_ids: Iterable[str] | None = None,
) -> None:
    """Bind an embedding fake to explicit workloads (all embedding seats by default).

    The default preserves the former repository-wide embedder fixture while
    still replacing only registered workload seats through the test provider.
    """
    if not hasattr(client, "configured"):
        try:
            client.configured = True
        except (AttributeError, TypeError):
            pass
    selected = tuple(workload_ids or (
        workload_id
        for workload_id, spec in WORKLOADS.items()
        if spec.kind == "embedding"
    ))
    for workload_id in selected:
        _bind_provider_client(repo, "embedding", workload_id, client)

    # Retrieval and Memory keep already-resolved adapters; refresh those
    # composed test seats without restoring a production setter.
    repo._runtime.set_embedder(client)
    if repo._runtime.source_embedding is not None:
        repo._runtime.source_embedding.embedder = repo._runtime.models.embedding
    if repo._runtime.memory_service is not None:
        repo._runtime.memory_service.embedder = client


def bind_rerank_client(repo: Any, client: Any) -> None:
    _bind_provider_client(repo, "rerank", "retrieval_rerank", client)
