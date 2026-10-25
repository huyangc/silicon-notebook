"""Explicit, result-preserving partition of the already selected G1 backend.

Timing hints influence ownership only. No environment state enables this plugin,
so child pytest invocations retain their complete collection.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.architecture import ci_partition

_module = ci_partition.module
_positive_number = ci_partition.positive_number
_unit = ci_partition.unit

_TIMINGS = Path(__file__).resolve().parents[1] / "fixtures/g1_module_timings.json"
# The second runner subsequently executes contracts/frontend, measured around
# five wall minutes on four workers. Express that load in cumulative test seconds.
CONTRACTS_FRONTEND_TEST_SECONDS = 5 * 60 * 4


def pytest_addoption(parser):
    group = parser.getgroup("G1 CI sharding")
    for option in ("index", "count"):
        group.addoption(f"--g1-shard-{option}", action="append", type=int, default=None)


def pytest_configure(config):
    ci_partition.validate_options(config, prefix="g1", lane="G1")


def _timings(path):
    return ci_partition.timings(path, lane="G1")


def _owners(units, costs, *, extra_seconds=CONTRACTS_FRONTEND_TEST_SECONDS):
    return ci_partition.owners(units, costs, extra_seconds=extra_seconds, lane="G1")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_collection_modifyitems(config, items):
    # Capture native nodeids before xdist mutates their hash and loadgroup suffix.
    # After yield, normal marker/keyword selection and dynamic groups are complete.
    native = {id(item): item.nodeid for item in items}
    yield
    ci_partition.partition(
        config, items, native, path=_TIMINGS,
        index=config.getoption("g1_shard_index")[0],
        extra_seconds=CONTRACTS_FRONTEND_TEST_SECONDS, lane="G1",
    )
