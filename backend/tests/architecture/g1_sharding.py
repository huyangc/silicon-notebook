"""Explicit, result-preserving partition of the already selected G1 backend.

Timing hints influence ownership only. No environment state enables this plugin,
so child pytest invocations retain their complete collection.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2]
_TIMINGS = Path(__file__).resolve().parents[1] / "fixtures/g1_module_timings.json"
# The second runner subsequently executes contracts/frontend, measured around
# five wall minutes on four workers. Express that load in cumulative test seconds.
CONTRACTS_FRONTEND_TEST_SECONDS = 5 * 60 * 4


def pytest_addoption(parser):
    group = parser.getgroup("G1 CI sharding")
    for option in ("index", "count"):
        group.addoption(f"--g1-shard-{option}", action="append", type=int, default=None)


def pytest_configure(config):
    indices = config.getoption("g1_shard_index")
    counts = config.getoption("g1_shard_count")
    if (
        not indices or len(indices) != 1 or not counts or len(counts) != 1
        or counts[0] != 2 or indices[0] not in (0, 1)
    ):
        raise pytest.UsageError(
            "G1 sharding requires exactly one paired index 0|1 and count 2"
        )


def _positive_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        return False


def _timings(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        default = data["default_seconds_per_test"]
        by_module = data["seconds_per_test"]
        if not _positive_number(default) or not isinstance(by_module, dict):
            raise ValueError("invalid default or module costs")
        if any(not isinstance(key, str) or not _positive_number(cost)
               for key, cost in by_module.items()):
            raise ValueError("invalid module cost")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise pytest.UsageError("G1 timing hints must contain finite positive costs") from error
    return by_module, default


def _module(item):
    try:
        return item.path.relative_to(_BACKEND).as_posix()
    except ValueError:
        return item.path.relative_to(item.config.rootpath).as_posix()


def _unit(item, native_nodeid):
    # xdist 3.8 appends sorted, unique marker names, then loadgroup uses the
    # final @ only when it follows the final ]. Joining markers can collide
    # (a_b vs a + b), and names themselves may contain @ or ]. Match that
    # effective scheduler key rather than assuming each marker is a group.
    names = {
        str(mark.args[0] if mark.args else mark.kwargs.get("name", "default"))
        for mark in item.iter_markers("xdist_group")
    }
    augmented = f"{native_nodeid}@{'_'.join(sorted(names))}" if names else native_nodeid
    if augmented.rfind("@") > augmented.rfind("]"):
        return ("group", augmented.rsplit("@", 1)[1])
    if names:
        return ("group", augmented)
    return ("module", _module(item))


def _owners(units, costs, *, extra_seconds=CONTRACTS_FRONTEND_TEST_SECONDS):
    loads = [0.0, float(extra_seconds)]
    owners = {}
    weighted = [(math.fsum(sorted(costs[item] for item in members)), key)
                for key, members in units.items()]
    for cost, key in sorted(weighted, key=lambda pair: (-pair[0], pair[1])):
        owner = min(range(2), key=lambda shard: (loads[shard], shard))
        owners[key] = owner
        loads[owner] += cost
    if set(owners.values()) != {0, 1}:
        raise pytest.UsageError("G1 timing allocation would produce an empty shard")
    return owners


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_collection_modifyitems(config, items):
    # Capture native nodeids before xdist appends its loadgroup suffix. The
    # post-yield boundary sees normal -m/-k deselection and dynamic conftest groups.
    # Item.__hash__ depends on nodeid, which xdist mutates in this hook.
    native = {id(item): item.nodeid for item in items}
    yield
    by_module, default = _timings(_TIMINGS)
    units = defaultdict(list)
    keys = {}
    costs = {}
    for item in items:
        keys[item] = _unit(item, native[id(item)])
        units[keys[item]].append(item)
        costs[item] = by_module.get(_module(item), default)
    owners = _owners(units, costs)
    index = config.getoption("g1_shard_index")[0]
    selected, deselected = [], []
    for item in items:
        (selected if owners[keys[item]] == index else deselected).append(item)
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)
