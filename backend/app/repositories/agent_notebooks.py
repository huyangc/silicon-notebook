"""Atomic creation port: token authority and its new notebook commit together."""
from typing import Any, Callable, Mapping, Protocol


class AgentNotebookStore(Protocol):
    def create_agent_notebook(
        self,
        token_id: str,
        authorize: Callable[[Mapping[str, Any] | None], None],
        create: Callable[[Any, str], str],
    ) -> str: ...
