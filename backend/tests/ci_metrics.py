"""Opt-in, content-free pytest phase and process metrics for CI observations.

Report durations arrive once at the xdist controller. Worker resource counters
travel in workeroutput, so neither test identifiers nor worker environments enter
the JSON artifact. These diagnostics never determine the test exit status.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

_WORKER_OUTPUT_KEY = "silicon_ci_metrics"
_PHASES = ("setup", "call", "teardown")


def _process_metrics() -> dict[str, float | int] | None:
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        # macOS reports bytes; Linux (the CI host) reports KiB.
        rss_bytes = int(usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024))
        return {
            "cpu_seconds": time.process_time(),
            "peak_rss_bytes": rss_bytes,
        }
    except Exception:  # noqa: BLE001 - unavailable observations cannot fail tests
        return None


def _cpu_capacity() -> int | None:
    try:
        if hasattr(os, "sched_getaffinity"):
            return len(os.sched_getaffinity(0))
        return os.cpu_count()
    except Exception:  # noqa: BLE001 - unavailable observations cannot fail tests
        return None


class CiMetrics:
    def __init__(self, config, destination: Path):
        self.config = config
        self.destination = destination
        self.started = time.perf_counter()
        self.cpu_started = time.process_time()
        self.phase_seconds = dict.fromkeys(_PHASES, 0.0)
        self.phase_reports = dict.fromkeys(_PHASES, 0)
        self.worker_count = 0
        self.worker_cpu_seconds = 0.0
        self.worker_peak_rss_bytes = 0
        self.worker_metrics_complete = True
        # Retain the nodes as well as their identities so an object id cannot
        # be reused while this session is still collecting observations.
        self._finished_nodes: dict[int, object] = {}

    def pytest_runtest_logreport(self, report):
        # Workers need only resource counters: their reports are forwarded to
        # the controller, which owns the aggregate to avoid double counting.
        if hasattr(self.config, "workerinput"):
            return
        try:
            if report.when in self.phase_seconds:
                self.phase_seconds[report.when] += report.duration
                self.phase_reports[report.when] += 1
        except Exception:  # noqa: BLE001 - observations never change test results
            self.worker_metrics_complete = False

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node, error):
        # xdist can report both workerfinished and errordown for the same
        # interrupted worker. Its later error still makes metrics incomplete.
        if error is not None:
            self.worker_metrics_complete = False
        identity = id(node)
        if identity in self._finished_nodes:
            return
        self._finished_nodes[identity] = node
        self.worker_count += 1
        try:
            metrics = node.workeroutput[_WORKER_OUTPUT_KEY]
            self.worker_cpu_seconds += metrics["cpu_seconds"]
            self.worker_peak_rss_bytes = max(
                self.worker_peak_rss_bytes, metrics["peak_rss_bytes"]
            )
        except Exception:  # noqa: BLE001 - crashed workers still retain test status
            self.worker_metrics_complete = False

    def pytest_sessionfinish(self, session, exitstatus):
        try:
            process = _process_metrics()
            if process is not None:
                process["cpu_seconds"] = max(
                    0.0, process["cpu_seconds"] - self.cpu_started
                )
            if hasattr(self.config, "workerinput"):
                self.config.workeroutput[_WORKER_OUTPUT_KEY] = process
                return
            parallel = self.worker_count > 0
            expected_workers = getattr(self.config.option, "numprocesses", 0)
            if expected_workers:
                parallel = True
                if isinstance(expected_workers, int) and expected_workers != self.worker_count:
                    self.worker_metrics_complete = False
            metrics = {
                "schema_version": 1,
                "wall_seconds": round(time.perf_counter() - self.started, 6),
                "cpu_capacity": _cpu_capacity(),
                "phase_sum_seconds": {
                    phase: round(seconds, 6)
                    for phase, seconds in self.phase_seconds.items()
                },
                "phase_reports": self.phase_reports,
                "worker_count": self.worker_count,
                "test_process_cpu_seconds": round(self.worker_cpu_seconds, 6)
                if parallel else round(process["cpu_seconds"], 6) if process else None,
                # This is the largest individual worker peak, never a claimed
                # simultaneous sum; it excludes the PostgreSQL service.
                "max_test_process_peak_rss_bytes": self.worker_peak_rss_bytes
                if parallel else process["peak_rss_bytes"] if process else None,
                "controller_cpu_seconds": process["cpu_seconds"]
                if parallel and process else None,
                "process_metrics_complete": self.worker_metrics_complete
                and process is not None,
            }
            encoded = json.dumps(metrics, sort_keys=True, allow_nan=False)
            self.destination.parent.mkdir(parents=True, exist_ok=True)
            self.destination.write_text(encoded + "\n", encoding="utf-8")
            reporter = self.config.pluginmanager.getplugin("terminalreporter")
            if reporter is not None:
                reporter.write_line("CI metrics: " + encoded)
        except Exception:  # noqa: BLE001 - even failed artifact writes are observational
            pass


def pytest_addoption(parser):
    parser.addoption(
        "--ci-metrics", type=Path, default=None,
        help="Write content-free phase and process measurements to this JSON file.",
    )


def pytest_configure(config):
    destination = config.getoption("--ci-metrics")
    if destination is not None:
        config.pluginmanager.register(CiMetrics(config, destination), "ci-metrics")
