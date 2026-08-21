from __future__ import annotations

import ast
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


def _graph_service_access_violations(text: str) -> list[str]:
    tree = ast.parse(text)
    graph_aliases = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "getattr"
            and len(node.value.args) >= 2
            and isinstance(node.value.args[1], ast.Constant)
            and node.value.args[1].value == "selected_source_graph"
        ):
            graph_aliases.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Name)
                and node.value.id in graph_aliases
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id not in graph_aliases:
                        graph_aliases.add(target.id)
                        changed = True

    def graph_value(node) -> bool:
        return (
            isinstance(node, ast.Name) and node.id in graph_aliases
        ) or (
            isinstance(node, ast.Attribute)
            and node.attr == "selected_source_graph"
        )

    violations = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in {"run", "fail_closed"}
            and graph_value(node.value)
        ):
            violations.append(node.attr)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and graph_value(node.args[0])
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in {"run", "fail_closed"}
        ):
            violations.append(str(node.args[1].value))
        if isinstance(node, ast.Call):
            bridge_call = (
                isinstance(node.func, ast.Name)
                and node.func.id == "SelectedSourceGraphContributionCall"
            )
            for index, argument in enumerate(node.args):
                if graph_value(argument) and not (bridge_call and index == 0):
                    violations.append("graph_service_forwarded")
    return violations


def test_ask_and_report_keep_other_host_output_when_graph_capability_is_absent():
    baseline = [SimpleNamespace(chunk_id="base")]
    appended = [*baseline, SimpleNamespace(chunk_id="plugin")]
    host = _RecordingHost(output=appended)
    connection_probe = SimpleNamespace(is_connection_held=lambda: False)

    ask = object.__new__(AskService)
    ask.retrieval_contributors = host
    ask.selected_source_graph = None
    ask.retrieval_connection_probe = connection_probe
    ask.retrieval_contributor_hydrate = lambda _notebook_id, _ids: ()
    ask.current_user_id = lambda: "actor"
    ask.settings = SimpleNamespace(selected_source_graph_enrichment_tokens=1)
    ask_chunks, ask_status = ask._activate_selected_source_graph("notebook", baseline)

    report = object.__new__(ReportEngine)
    report.dependencies = SimpleNamespace(
        retrieval_contributors=host,
        selected_source_graph=None,
        retrieval_connection_probe=connection_probe,
        retrieval_contributor_hydrate=lambda _notebook_id, _ids: (),
    )
    report.settings = SimpleNamespace(
        ppr_top_chunks=1,
        selected_source_graph_enrichment_tokens=1,
    )
    report.user_id = "actor"
    report.cancel_event = None
    result = SimpleNamespace(chunks=baseline)
    report._activate_selected_source_graph("notebook", result)

    assert ask_chunks == appended
    assert ask_status is None
    assert result.chunks == appended
    assert not hasattr(result, "baseline_chunks")
    assert [invocation for _baseline, invocation in host.calls] == [
        "selected_evidence",
        "selected_evidence",
    ]


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
        assert _graph_service_access_violations(text) == []


def test_graph_service_direct_call_guard_catches_alias_and_forwarding_mutations():
    mutations = (
        "self.selected_source_graph.run()",
        "service = getattr(self, 'selected_source_graph'); service.fail_closed()",
        "service = getattr(self, 'selected_source_graph'); runner = service.run; runner()",
        "service = getattr(self, 'selected_source_graph'); getattr(service, 'run')()",
        "service = getattr(self, 'selected_source_graph'); helper(service)",
    )
    assert all(_graph_service_access_violations(text) for text in mutations)


def test_report_runs_selected_evidence_without_graph_service():
    original = [SimpleNamespace(chunk_id="base")]
    appended = [*original, SimpleNamespace(chunk_id="plugin")]
    host = _RecordingHost(output=appended)
    report = object.__new__(ReportEngine)
    report.dependencies = SimpleNamespace(
        retrieval_contributors=host,
        selected_source_graph=None,
        retrieval_connection_probe=SimpleNamespace(
            is_connection_held=lambda: False
        ),
        retrieval_contributor_hydrate=lambda _notebook_id, _ids: (),
    )
    report.settings = SimpleNamespace(
        ppr_top_chunks=1,
        selected_source_graph_enrichment_tokens=1,
    )
    report.user_id = "actor"
    report.cancel_event = None
    result = SimpleNamespace(chunks=original)

    report._activate_selected_source_graph("notebook", result)

    assert not hasattr(result, "baseline_chunks")
    assert result.chunks == appended
    assert host.calls == [(original, "selected_evidence")]


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
