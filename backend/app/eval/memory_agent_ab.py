"""Three-way Agent evaluation over identical task fixtures."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import json
from typing import Any


MODES = ("no_memory", "kb_only", "kb_confirmed_memory")
FIXED_TASKS = (
    {"id": "reuse-confirmed-timing", "kb_succeeds": False, "memory_succeeds": True},
    {"id": "answer-from-kb", "kb_succeeds": True, "memory_succeeds": True},
    {"id": "avoid-repeated-investigation", "kb_succeeds": False, "memory_succeeds": True},
)


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


def _fixed_runner(task: Mapping[str, Any], mode: str) -> Mapping[str, Any]:
    success = (
        bool(task.get("kb_succeeds")) if mode == "kb_only"
        else bool(task.get("memory_succeeds")) if mode == "kb_confirmed_memory"
        else False
    )
    return {
        "success": success,
        "tool_calls": 1 if success else 2,
        "repeated_steps": 0 if success else 1,
        "token_count": 80 if success else 140,
        "citation_valid": True,
    }


def run_fixed_agent_ab() -> dict[str, dict[str, float | int]]:
    return run_agent_ab(FIXED_TASKS, runner=_fixed_runner)


if __name__ == "__main__":  # pragma: no cover - manual evaluation entrypoint
    print(json.dumps(run_fixed_agent_ab(), ensure_ascii=False, indent=2))
