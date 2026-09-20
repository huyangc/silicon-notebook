"""Opt-in CI partitioning after the normal G1 collection and marker selection.

Only check_backend.sh's explicit CLI options load this plugin. Nested pytest
processes inherit neither the plugin nor a shard index. Timing hints only balance
already-selected items; missing or stale entries cannot remove coverage.
"""
from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path

import pytest


_BACKEND = Path(__file__).resolve().parents[2]
_TIMINGS = Path(__file__).resolve().parents[1] / "fixtures/g1_module_timings.json"


def pytest_addoption(parser):
    group = parser.getgroup("G1 CI sharding")
    group.addoption("--g1-shard-index", type=int, default=None)
    group.addoption("--g1-shard-count", type=int, default=None)


def pytest_configure(config):
    index = config.getoption("g1_shard_index")
    count = config.getoption("g1_shard_count")
    if index is None or count is None or count < 1 or not 0 <= index < count:
        raise pytest.UsageError(
            "G1 sharding requires both options with 0 <= shard-index < shard-count"
        )


def _module(item: pytest.Item) -> str:
    path = item.path
    try:
        return path.relative_to(_BACKEND).as_posix()
    except ValueError:
        return path.relative_to(item.config.rootpath).as_posix()


def _unit(item: pytest.Item) -> tuple[str, str]:
    # Match xdist's effective loadgroup key, including combined markers and
    # the default name. A group may span modules and must stay in one shard.
    groups = {
        str(mark.args[0] if mark.args else mark.kwargs.get("name", "default"))
        for mark in item.iter_markers("xdist_group")
    }
    if groups:
        return ("group", "_".join(sorted(groups)))
    return ("module", _module(item))


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_collection_modifyitems(config, items):
    # Wrap rather than merely trylast: repository conftests assign groups and
    # pytest removes -m/-k deselections before we partition the surviving list.
    yield
    index = config.getoption("g1_shard_index")
    count = config.getoption("g1_shard_count")
    timings = json.loads(_TIMINGS.read_text(encoding="utf-8"))
    by_module = timings["seconds_per_test"]
    default = timings["default_seconds_per_test"]
    units = defaultdict(list)
    for item in items:
        units[_unit(item)].append(item)
    if len(units) < count:
        raise pytest.UsageError(
            f"G1 collection has {len(units)} indivisible units for {count} shards; "
            "refusing an empty shard"
        )
    weighted = [
        (sum(sorted(by_module.get(_module(item), default) for item in members)), key)
        for key, members in units.items()
    ]
    loads = [0.0] * count
    owners = {}
    for cost, key in sorted(weighted, key=lambda pair: (-pair[0], pair[1])):
        owner = min(range(count), key=lambda shard: (loads[shard], shard))
        owners[key] = owner
        loads[owner] += cost
    selected = []
    deselected = []
    for item in items:
        (selected if owners[_unit(item)] == index else deselected).append(item)
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)
