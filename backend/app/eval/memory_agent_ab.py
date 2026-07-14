"""Three-way deterministic Agent harness over identical task fixtures."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import json
from typing import Any

from app.eval.memory_retrieval import _fixture_retriever
from app.services.retrieval import keyword_score


MODES = ("no_memory", "kb_only", "kb_confirmed_memory")
FIXED_TASKS = (
    {
        "id": "reuse-confirmed-timing",
        "query": "timing closure",
        "expected": ["memory-confirmed-timing"],
    },
    {
        "id": "answer-from-kb",
        "query": "clock uncertainty",
        "expected": ["kb-clock-uncertainty"],
    },
    {
        "id": "avoid-repeated-investigation",
        "query": "avoid repeated investigation",
        "expected": ["memory-confirmed-repeat"],
    },
)

_FIXTURE_KB = {
    "kb-clock-uncertainty": "Clock uncertainty must be included in setup analysis.",
}


def run_agent_ab(
    tasks: Iterable[Mapping[str, Any]],
    *,
    runner: Callable[[Mapping[str, Any], str], Mapping[str, Any]],
) -> dict[str, dict[str, float | int]]:
    fixed_tasks = [dict(task) for task in tasks]
    result: dict[str, dict[str, float | int]] = {}
    for mode in MODES:
        rows = [dict(runner(task, mode)) for task in fixed_tasks]
        count = len(rows) or 1
        result[mode] = {
            "success_rate": sum(bool(row.get("success")) for row in rows) / count,
            "tool_calls": sum(int(row.get("tool_calls", 0)) for row in rows),
            "repeated_steps": sum(int(row.get("repeated_steps", 0)) for row in rows),
            "token_count": sum(int(row.get("token_count", 0)) for row in rows),
            "citation_validity": sum(bool(row.get("citation_valid")) for row in rows) / count,
        }
    return result


class _FixtureAgentHarness:
    def __init__(self) -> None:
        self.memory_retriever, _scope = _fixture_retriever()

    @staticmethod
    def _kb_hits(query: str) -> list[tuple[str, str]]:
        return [
            (item_id, text)
            for item_id, text in _FIXTURE_KB.items()
            if keyword_score(query, text) > 0
        ]

    def run(self, task: Mapping[str, Any], mode: str) -> Mapping[str, Any]:
        query = str(task["query"])
        evidence: list[tuple[str, str]] = []
        tool_calls = 0
        if mode in {"kb_only", "kb_confirmed_memory"}:
            evidence.extend(self._kb_hits(query))
            tool_calls += 1
        if mode == "kb_confirmed_memory":
            memory_hits = self.memory_retriever.notebook_memory_hits(
                "alice", "timing", query, 5
            )
            evidence.extend((hit.memory_id, hit.text) for hit in memory_hits)
            tool_calls += 1

        available_ids = {item_id for item_id, _text in evidence}
        expected = {str(item) for item in task.get("expected", [])}
        cited_ids = sorted(expected.intersection(available_ids))
        success = bool(cited_ids)
        token_count = sum((len(text) + 3) // 4 for _item_id, text in evidence)
        return {
            "success": success,
            "tool_calls": tool_calls,
            "repeated_steps": 0 if success else 1,
            "token_count": token_count,
            "citation_valid": all(item_id in available_ids for item_id in cited_ids),
        }


def run_fixed_agent_ab() -> dict[str, dict[str, float | int]]:
    harness = _FixtureAgentHarness()
    return run_agent_ab(FIXED_TASKS, runner=harness.run)


if __name__ == "__main__":  # pragma: no cover - manual evaluation entrypoint
    print(json.dumps(run_fixed_agent_ab(), ensure_ascii=False, indent=2))
