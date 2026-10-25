"""Shared result-preserving allocation of normally selected pytest items."""
from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2]


def positive_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        return False


def timings(path, *, lane):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        default = data["default_seconds_per_test"]
        by_module = data["seconds_per_test"]
        if not positive_number(default) or not isinstance(by_module, dict):
            raise ValueError("invalid default or module costs")
        if any(not isinstance(key, str) or not positive_number(cost)
               for key, cost in by_module.items()):
            raise ValueError("invalid module cost")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise pytest.UsageError(f"{lane} timing hints must contain finite positive costs") from error
    return by_module, default


def module(item):
    try:
        return item.path.relative_to(_BACKEND).as_posix()
    except ValueError:
        return item.path.relative_to(item.config.rootpath).as_posix()


def unit(item, native_nodeid):
    # Match xdist 3.8's effective scheduler key, including combined-name
    # collisions and marker names containing @ or ].
    names = {
        str(mark.args[0] if mark.args else mark.kwargs.get("name", "default"))
        for mark in item.iter_markers("xdist_group")
    }
    augmented = f"{native_nodeid}@{'_'.join(sorted(names))}" if names else native_nodeid
    if augmented.rfind("@") > augmented.rfind("]"):
        return ("group", augmented.rsplit("@", 1)[1])
    if names:
        return ("group", augmented)
    return ("module", module(item))


def owners(units, costs, *, extra_seconds, lane):
    loads = [0.0, float(extra_seconds)]
    ownership = {}
    weighted = [(math.fsum(sorted(costs[item] for item in members)), key)
                for key, members in units.items()]
    for cost, key in sorted(weighted, key=lambda pair: (-pair[0], pair[1])):
        owner = min(range(2), key=lambda shard: (loads[shard], shard))
        ownership[key] = owner
        loads[owner] += cost
    if set(ownership.values()) != {0, 1}:
        raise pytest.UsageError(f"{lane} timing allocation would produce an empty shard")
    return ownership


def validate_options(config, *, prefix, lane):
    indices = config.getoption(f"{prefix}_shard_index")
    counts = config.getoption(f"{prefix}_shard_count")
    if (
        not indices or len(indices) != 1 or not counts or len(counts) != 1
        or counts[0] != 2 or indices[0] not in (0, 1)
    ):
        raise pytest.UsageError(
            f"{lane} sharding requires exactly one paired index 0|1 and count 2"
        )


def partition(config, items, native, *, path, index, extra_seconds, lane):
    by_module, default = timings(path, lane=lane)
    units = defaultdict(list)
    keys = {}
    costs = {}
    for item in items:
        keys[item] = unit(item, native[id(item)])
        units[keys[item]].append(item)
        costs[item] = by_module.get(module(item), default)
    ownership = owners(units, costs, extra_seconds=extra_seconds, lane=lane)
    selected, deselected = [], []
    for item in items:
        (selected if ownership[keys[item]] == index else deselected).append(item)
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)
