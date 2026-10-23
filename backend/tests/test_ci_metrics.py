"""Hermetic behavior checks for the observational pytest metrics plugin."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import ci_metrics


def _config(*, workers=0, worker=False):
    lines = []
    reporter = SimpleNamespace(write_line=lines.append)
    config = SimpleNamespace(
        option=SimpleNamespace(numprocesses=workers),
        pluginmanager=SimpleNamespace(getplugin=lambda _name: reporter),
    )
    if worker:
        config.workerinput = {}
        config.workeroutput = {}
    return config, lines


@pytest.fixture
def controlled_clock(monkeypatch):
    clock = SimpleNamespace(wall=100.0, cpu=10.0)
    monkeypatch.setattr(ci_metrics.time, "perf_counter", lambda: clock.wall)
    monkeypatch.setattr(ci_metrics.time, "process_time", lambda: clock.cpu)
    monkeypatch.setattr(ci_metrics, "_cpu_capacity", lambda: 4)
    monkeypatch.setattr(ci_metrics, "_process_metrics", lambda: {
        "cpu_seconds": clock.cpu,
        "peak_rss_bytes": 2048,
    })
    return clock


def _report(when, duration):
    return SimpleNamespace(
        when=when, duration=duration,
        nodeid="secret/private/source.py::evidence[prompt]",
        longrepr="postgresql://user:secret@host/private_database",
    )


def test_serial_phases_include_failure_teardown_without_recording_test_content(
    tmp_path, controlled_clock,
):
    config, lines = _config()
    destination = tmp_path / "metrics" / "lane.json"
    plugin = ci_metrics.CiMetrics(config, destination)
    # One passing test and one setup failure: no synthetic call phase is added.
    for phase, duration in (
        ("setup", 2), ("call", 3), ("teardown", 1),
        ("setup", 4), ("teardown", 0.5),
    ):
        plugin.pytest_runtest_logreport(_report(phase, duration))
    controlled_clock.wall += 12
    controlled_clock.cpu += 5
    session = SimpleNamespace(exitstatus=1)
    plugin.pytest_sessionfinish(session, 1)

    metrics = json.loads(destination.read_text())
    assert metrics["wall_seconds"] == 12
    assert metrics["phase_sum_seconds"] == {"setup": 6, "call": 3, "teardown": 1.5}
    assert metrics["phase_reports"] == {"setup": 2, "call": 1, "teardown": 2}
    assert metrics["test_process_cpu_seconds"] == 5
    assert metrics["max_test_process_peak_rss_bytes"] == 2048
    assert metrics["worker_count"] == 0
    assert metrics["cpu_capacity"] == 4
    assert metrics["process_metrics_complete"] is True
    assert session.exitstatus == 1
    assert len(lines) == 1
    assert "secret" not in lines[0]
    assert "private" not in destination.read_text()


def test_xdist_reports_are_counted_once_and_worker_resources_are_aggregated(
    tmp_path, controlled_clock,
):
    config, _ = _config(workers=2)
    destination = tmp_path / "metrics.json"
    controller = ci_metrics.CiMetrics(config, destination)
    for worker_seconds in (4, 6):
        worker_config, _ = _config(workers=2, worker=True)
        worker = ci_metrics.CiMetrics(worker_config, destination)
        for phase in ("setup", "call", "teardown"):
            report = _report(phase, 2)
            worker.pytest_runtest_logreport(report)
            controller.pytest_runtest_logreport(report)
        controlled_clock.cpu += worker_seconds
        worker.pytest_sessionfinish(SimpleNamespace(), 0)
        assert not destination.exists()
        assert worker.phase_seconds == {"setup": 0, "call": 0, "teardown": 0}
        controller.pytest_testnodedown(worker_config, None)
    controlled_clock.wall += 8
    controller.pytest_sessionfinish(SimpleNamespace(), 0)

    metrics = json.loads(destination.read_text())
    assert metrics["phase_sum_seconds"] == {"setup": 4, "call": 4, "teardown": 4}
    assert metrics["wall_seconds"] == 8
    assert metrics["worker_count"] == 2
    assert metrics["test_process_cpu_seconds"] == 10
    assert metrics["max_test_process_peak_rss_bytes"] == 2048
    assert metrics["process_metrics_complete"] is True


@pytest.mark.parametrize("duplicate_error", [None, "keyboard-interrupt"])
def test_repeated_node_shutdown_counts_resources_once_but_keeps_later_error(
    tmp_path, controlled_clock, duplicate_error,
):
    config, _ = _config(workers=1)
    destination = tmp_path / "metrics.json"
    plugin = ci_metrics.CiMetrics(config, destination)
    node = SimpleNamespace(workeroutput={
        ci_metrics._WORKER_OUTPUT_KEY: {"cpu_seconds": 7, "peak_rss_bytes": 4096},
    })
    plugin.pytest_testnodedown(node, None)
    plugin.pytest_testnodedown(node, duplicate_error)
    plugin.pytest_sessionfinish(SimpleNamespace(), 2 if duplicate_error else 0)

    metrics = json.loads(destination.read_text())
    assert metrics["worker_count"] == 1
    assert metrics["test_process_cpu_seconds"] == 7
    assert metrics["max_test_process_peak_rss_bytes"] == 4096
    assert metrics["process_metrics_complete"] is (duplicate_error is None)


def test_crashed_worker_marks_incomplete_without_leaking_its_exception(
    tmp_path, controlled_clock,
):
    config, lines = _config(workers=2)
    destination = tmp_path / "metrics.json"
    plugin = ci_metrics.CiMetrics(config, destination)
    plugin.pytest_testnodedown(
        SimpleNamespace(workeroutput={}), RuntimeError("secret pgpass path"),
    )
    plugin.pytest_sessionfinish(SimpleNamespace(), 1)

    metrics = json.loads(destination.read_text())
    assert metrics["process_metrics_complete"] is False
    assert metrics["worker_count"] == 1
    assert "secret" not in lines[0]


def test_unavailable_process_metrics_preserve_phase_observations(
    tmp_path, controlled_clock, monkeypatch,
):
    monkeypatch.setattr(ci_metrics, "_process_metrics", lambda: None)
    config, _ = _config()
    destination = tmp_path / "metrics.json"
    plugin = ci_metrics.CiMetrics(config, destination)
    plugin.pytest_runtest_logreport(_report("call", 2))
    plugin.pytest_sessionfinish(SimpleNamespace(), 0)

    metrics = json.loads(destination.read_text())
    assert metrics["phase_sum_seconds"]["call"] == 2
    assert metrics["test_process_cpu_seconds"] is None
    assert metrics["max_test_process_peak_rss_bytes"] is None
    assert metrics["process_metrics_complete"] is False


def test_output_failure_is_observational_and_does_not_disclose_paths(
    tmp_path, controlled_clock, monkeypatch, capsys,
):
    config, lines = _config()
    plugin = ci_metrics.CiMetrics(config, tmp_path / "metrics.json")

    def fail_write(*_args, **_kwargs):
        raise PermissionError("secret/private/evidence")

    monkeypatch.setattr(Path, "write_text", fail_write)
    session = SimpleNamespace(exitstatus=1)
    plugin.pytest_sessionfinish(session, 1)
    assert session.exitstatus == 1
    assert lines == []
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("destination", [None, Path("lane.json")])
def test_plugin_stays_disabled_unless_an_output_is_requested(destination):
    registered = []
    config = SimpleNamespace(
        getoption=lambda _name: destination,
        pluginmanager=SimpleNamespace(register=lambda *args: registered.append(args)),
    )
    ci_metrics.pytest_configure(config)
    assert len(registered) == (0 if destination is None else 1)


@pytest.mark.parametrize("platform, multiplier", [("darwin", 1), ("linux", 1024)])
def test_resource_rss_units_are_normalized_to_bytes(monkeypatch, platform, multiplier):
    import resource

    monkeypatch.setattr(ci_metrics.sys, "platform", platform)
    monkeypatch.setattr(resource, "getrusage", lambda _who: SimpleNamespace(ru_maxrss=123))
    assert ci_metrics._process_metrics()["peak_rss_bytes"] == 123 * multiplier
