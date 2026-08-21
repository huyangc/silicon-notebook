from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.core.config import Settings
from app.services.ask_service import AskService
from app.services.report_engine import ReportEngine


class _RecordingHost:
    def __init__(self, output=None) -> None:
        self.calls = []
        self.output = output

    def run(self, baseline, *, invocation, **_kwargs):
        self.calls.append((baseline, invocation))
        return baseline if self.output is None else self.output


def test_ask_and_report_skip_host_when_graph_capability_is_absent():
    host = _RecordingHost()
    baseline = [SimpleNamespace(chunk_id="base")]

    ask = object.__new__(AskService)
    ask.retrieval_contributors = host
    ask.selected_source_graph = None
    ask_chunks, ask_status = ask._activate_selected_source_graph("notebook", baseline)

    report = object.__new__(ReportEngine)
    report.dependencies = SimpleNamespace(
        retrieval_contributors=host,
        selected_source_graph=None,
    )
    result = SimpleNamespace(chunks=baseline)
    report._activate_selected_source_graph("notebook", result)

    assert ask_chunks == baseline
    assert ask_status is None
    assert result.chunks is baseline
    assert host.calls == []


def test_application_bootstrap_injects_process_shared_retrieval_host(monkeypatch):
    from app import bootstrap

    host = object()
    runtime = SimpleNamespace(retrieval_contributors=host)
    captured = {}

    def fake_repository(settings, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(settings=settings)

    monkeypatch.setattr(bootstrap, "application_extension_runtime", lambda: runtime)
    monkeypatch.setattr(bootstrap, "create_repository", fake_repository)

    created = bootstrap.create_application_repository(
        Settings(database_url="sqlite:////tmp/retrieval-contributor-wiring.db")
    )

    assert created.settings.database_url.startswith("sqlite")
    assert captured == {"retrieval_contributor_host": host}


def test_default_topology_registers_one_atomic_selected_graph_contributor():
    from app.extensions import default_extension_runtime
    from app.extensions.builtin import SELECTED_SOURCE_GRAPH_CONTRIBUTION_ID

    runtime = default_extension_runtime()
    contributions = runtime.registry.contributions("retrieval.contributor")

    assert [item.contribution.declaration.id for item in contributions] == [
        SELECTED_SOURCE_GRAPH_CONTRIBUTION_ID
    ]
    frozen = runtime.retrieval_contributors._registrations
    assert len(frozen) == 1
    assert frozen[0].admission == "atomic"


def test_ask_and_report_no_longer_call_graph_service_directly():
    services = Path(__file__).resolve().parents[1] / "app" / "services"
    for name in ("ask_service.py", "report_engine.py"):
        text = (services / name).read_text(encoding="utf-8")
        assert "selected_source_graph.run(" not in text
        assert "selected_source_graph.fail_closed(" not in text


def test_report_does_not_run_selected_evidence_without_graph_service():
    original = [SimpleNamespace(chunk_id="base")]
    appended = [*original, SimpleNamespace(chunk_id="plugin")]
    host = _RecordingHost(output=appended)
    report = object.__new__(ReportEngine)
    report.dependencies = SimpleNamespace(
        retrieval_contributors=host,
        selected_source_graph=None,
    )
    result = SimpleNamespace(chunks=original)

    report._activate_selected_source_graph("notebook", result)

    assert not hasattr(result, "baseline_chunks")
    assert result.chunks == original
    assert host.calls == []


def test_repository_factory_accepts_injected_host_without_importing_registry(monkeypatch):
    from app.repositories import factory

    host = object()
    captured = {}

    def fake_repository(settings, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(settings=settings)

    monkeypatch.setattr(factory, "SQLiteRepository", fake_repository)
    factory.create_repository(
        Settings(database_url="sqlite:////tmp/retrieval-host-factory.db"),
        retrieval_contributor_host=host,
    )

    assert captured == {"retrieval_contributor_host": host}


def test_factory_created_ask_and_report_share_builtin_host(tmp_path):
    from app.bootstrap import (
        application_extension_runtime,
        create_application_repository,
    )

    repository = create_application_repository(Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'wiring.db'}",
        storage_dir=str(tmp_path / "storage"),
        event_log_enabled=False,
        llm_log_enabled=False,
    ))
    try:
        host = application_extension_runtime().retrieval_contributors
        ask = repository._runtime.ask_service()
        report = repository._runtime.report_execution.engine_factory(
            user_id="wiring-review"
        )

        assert repository._runtime.retrieval_contributors is host
        assert ask.retrieval_contributors is host
        assert report.dependencies.retrieval_contributors is host

        baseline = [SimpleNamespace(chunk_id="base")]
        ask.selected_source_graph = None
        object.__setattr__(
            report.dependencies, "selected_source_graph", None
        )
        ask_chunks, ask_status = ask._activate_selected_source_graph(
            "notebook", baseline
        )
        report_result = SimpleNamespace(chunks=baseline)
        report._activate_selected_source_graph("notebook", report_result)

        assert ask_chunks == baseline
        assert ask_status is None
        assert report_result.chunks is baseline
    finally:
        repository.close()
