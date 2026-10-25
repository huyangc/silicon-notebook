"""Opt-in PostgreSQL allocation; timing hints never determine test membership."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.architecture import ci_partition

_TIMINGS = Path(__file__).resolve().parents[1] / "fixtures/pg_module_timings.json"
# The second PG runner first completes its G1 backend shard on four workers.
BACKEND_PRIMARY_TEST_SECONDS = 9 * 60 * 4


def pytest_addoption(parser):
    group = parser.getgroup("PostgreSQL CI sharding")
    for option in ("index", "count"):
        group.addoption(f"--pg-shard-{option}", action="append", type=int, default=None)


def pytest_configure(config):
    ci_partition.validate_options(config, prefix="pg", lane="PostgreSQL")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_collection_modifyitems(config, items):
    native = {id(item): item.nodeid for item in items}
    yield
    ci_partition.partition(
        config, items, native, path=_TIMINGS,
        index=config.getoption("pg_shard_index")[0],
        extra_seconds=BACKEND_PRIMARY_TEST_SECONDS, lane="PostgreSQL",
    )
