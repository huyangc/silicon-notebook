"""Explicit workload-level model fakes for repository integration tests."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass
class RecordingModelProvider:
    chat_clients: Mapping[str, Any] = field(default_factory=dict)
    embedding_clients: Mapping[str, Any] = field(default_factory=dict)
    rerank_clients: Mapping[str, Any] = field(default_factory=dict)
    parallelism_by_workload: Mapping[str, int] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    closed: int = 0

    def chat(self, workload_id: str) -> Any:
        self.calls.append(("chat", workload_id))
        return self.chat_clients[workload_id]

    def embedding(self, workload_id: str) -> Any:
        self.calls.append(("embedding", workload_id))
        return self.embedding_clients[workload_id]

    def rerank(self, workload_id: str) -> Any:
        self.calls.append(("rerank", workload_id))
        return self.rerank_clients[workload_id]

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

    def probe(self, service_id: str, *, actor_id: str, allow_half_open: bool):
        self.calls.append(("probe", service_id))
        raise NotImplementedError("RecordingModelProvider probes must be explicit")

    def scheduler_snapshot(self, service_id: str):
        self.calls.append(("snapshot", service_id))
        raise NotImplementedError("RecordingModelProvider snapshots must be explicit")

    def close(self) -> None:
        self.closed += 1
