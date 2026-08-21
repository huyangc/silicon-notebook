from __future__ import annotations

from types import SimpleNamespace

from app.core.config import Settings
from app.services.ask_service import AskService
from app.services.report_engine import ReportEngine


class _RecordingHost:
    def __init__(self) -> None:
        self.calls = []

    def run(self, baseline, *, invocation, **_kwargs):
        self.calls.append((baseline, invocation))
        return baseline


def test_ask_and_report_call_same_host_at_selected_evidence_boundary():
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
    assert host.calls == [
        (baseline, "selected_evidence"),
        (baseline, "selected_evidence"),
    ]


def test_repository_factory_injects_process_shared_retrieval_host(monkeypatch):
    from app.repositories import factory

    host = object()
    runtime = SimpleNamespace(retrieval_contributors=host)
    captured = {}

    def fake_repository(settings, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(settings=settings)

    monkeypatch.setattr(factory, "default_extension_runtime", lambda: runtime)
    monkeypatch.setattr(factory, "SQLiteRepository", fake_repository)

    created = factory.create_repository(
        Settings(database_url="sqlite:////tmp/retrieval-contributor-wiring.db")
    )

    assert created.settings.database_url.startswith("sqlite")
    assert captured == {"retrieval_contributor_host": host}
