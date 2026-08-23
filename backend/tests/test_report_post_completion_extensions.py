from __future__ import annotations

from dataclasses import dataclass
import time

from app.domain.extensions import (
    CompletedReportNotification,
    ReportCompletedObserverCallContext,
)
from app.extension_sdk import (
    EXTENSION_API_VERSION,
    REPORT_COMPLETED_OBSERVER_POINT,
    REPORT_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY,
    ContributionDeclaration,
    ContributionKind,
    Availability,
    ExtensionContribution,
    ExtensionManifest,
    ExtensionRegistrar,
)
from app.extensions.bootstrap import build_extension_runtime, default_extension_runtime
from app.application.report_pipeline import CommittedReport


class _Probe:
    def __init__(self, held=False):
        self.held = held
        self.calls = 0

    def is_connection_held(self):
        self.calls += 1
        return self.held


class _Port:
    def __init__(self, calls):
        self.calls = calls

    def notify(self):
        self.calls.append("profile")


@dataclass(frozen=True)
class _Bundle:
    manifest: ExtensionManifest
    contribution: ExtensionContribution

    def register(self, registrar: ExtensionRegistrar):
        registrar.add(self.contribution)


def _deadline():
    return time.monotonic() + 30.0


def _context(calls, *, probe=None):
    return ReportCompletedObserverCallContext(
        notification=CompletedReportNotification("rep", "actor", "nb", "done"),
        agent_profile=_Port(calls),
        connection_probe=probe or _Probe(),
        deadline_monotonic=_deadline(),
    )


def test_empty_report_observer_is_strict_noop_before_any_collaborator():
    def poison(*_args, **_kwargs):
        raise AssertionError("empty host touched a collaborator")

    runtime = build_extension_runtime((), event_sink=poison)
    runtime.report_completed_observers._clock = poison
    assert runtime.report_completed_observers.observe_application(
        object(), event_sink=poison
    ) is None


def test_default_report_observer_calls_core_once_and_emits_content_free_event():
    runtime = default_extension_runtime()
    calls = []
    events = []
    runtime.report_completed_observers.observe_application(
        _context(calls), event_sink=events.append
    )
    assert calls == ["profile"]
    assert len(events) == 1
    assert set(events[0]) <= {
        "kind", "point", "plugin_id", "contribution_id", "status",
        "duration_ms", "count", "reason_code",
    }
    assert not ({"actor", "notebook", "report_id", "content"} & set(events[0]))


def test_report_observer_is_inert_when_connection_is_held():
    calls = []
    default_extension_runtime().report_completed_observers.observe_application(
        _context(calls, probe=_Probe(True))
    )
    assert calls == []


def test_generic_report_observer_receives_only_the_opaque_report_ref():
    seen = []

    class Observer:
        def observe(self, context):
            seen.append(context)
            from app.extension_sdk import ExtensionResultStatus, ObserverReceipt
            return ObserverReceipt(ExtensionResultStatus.AVAILABLE)

    declaration = ContributionDeclaration(
        "observer.report_index", REPORT_COMPLETED_OBSERVER_POINT,
        ContributionKind.OBSERVER,
    )
    bundle = _Bundle(
        ExtensionManifest(
            id="observer.report_index",
            version="1.0.0",
            api_version=EXTENSION_API_VERSION,
            display_name="report index",
            trust="builtin",
            contributions=(declaration,),
        ),
        ExtensionContribution(declaration, Observer()),
    )
    build_extension_runtime((bundle,)).report_completed_observers.observe_application(
        _context([])
    )
    assert seen[0].report.id == "rep"
    assert seen[0].report.terminal_status == "done"
    assert seen[0].actor is None
    assert seen[0].notebook is None
    assert seen[0].access is None


def test_application_runtime_wires_report_completion_to_existing_profile_job(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'db.sqlite'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.bootstrap import create_application_repository
    from app.core.config import Settings

    repo = create_application_repository(Settings(_env_file=None))
    runtime = repo._runtime
    calls = []
    events = []
    monkeypatch.setattr(
        runtime.agent_profile_jobs,
        "note_report_completed",
        lambda notebook_id, actor_id: calls.append((notebook_id, actor_id)),
    )
    monkeypatch.setattr(runtime.event_log, "emit", events.append)
    runtime._after_report_completed(CommittedReport("nb", "rep", "actor"))

    assert calls == [("nb", "actor")]
    assert [event["kind"] for event in events] == ["report_extension"]
    assert runtime.report_execution.after_completed == runtime._after_report_completed


def test_composed_terminal_cas_drives_exactly_one_profile_signal(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'db.sqlite'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.bootstrap import create_application_repository
    from app.core.config import Settings
    from app.models.schemas import NotebookCreate
    from app.services.report_execution import (
        ReportCancellationRegistry,
        ReportExecutionCoordinator,
    )

    repo = create_application_repository(Settings(_env_file=None))
    runtime = repo._runtime
    notebook = runtime.catalog.create_notebook(NotebookCreate(name="report"))
    signals = []
    monkeypatch.setattr(
        runtime.agent_profile_jobs,
        "note_report_completed",
        lambda notebook_id, actor_id: signals.append((notebook_id, actor_id)),
    )

    class Engine:
        def __init__(self, lose):
            self.lose = lose

        def generate(self, notebook_id, report_id, _question, depth=2):
            if self.lose:
                assert runtime.report_store.cancel_report(notebook_id, report_id)
            committed = runtime.report_store.complete_report_generation(
                notebook_id,
                report_id,
                sections=[{"markdown": "body"}],
                content_md="body",
                gaps=[],
                references=[],
            )
            return (
                CommittedReport(notebook_id, report_id, "actor")
                if committed else None
            )

    lose_next = {"value": False}
    coordinator = ReportExecutionCoordinator(
        reports=runtime.report_store,
        engine_factory=lambda **_kwargs: Engine(lose_next["value"]),
        cancellations=ReportCancellationRegistry(),
        job_submitter=lambda fn, **_kwargs: fn(),
        after_completed=runtime._after_report_completed,
    )

    first = runtime.report_store.create_report(notebook.id, "q")
    runtime.report_store.update_report(notebook.id, first, status="outline_ready")
    assert runtime.report_store.claim_report_generation(notebook.id, first)
    assert coordinator.start_generate(
        notebook.id, first, "q", user_id="actor"
    )

    lose_next["value"] = True
    second = runtime.report_store.create_report(notebook.id, "q2")
    runtime.report_store.update_report(notebook.id, second, status="outline_ready")
    assert runtime.report_store.claim_report_generation(notebook.id, second)
    assert coordinator.start_generate(
        notebook.id, second, "q2", user_id="actor"
    )

    assert signals == [(notebook.id, "actor")]
    assert runtime.report_store.get_report(notebook.id, first)["status"] == "done"
    assert runtime.report_store.get_report(notebook.id, second)["status"] == "cancelled"


def test_report_hosts_stop_before_later_plugins_when_callback_leaves_a_lease():
    probe = _Probe()
    calls = []
    events = []

    class FirstObserver:
        def observe(self, context):
            probe.held = True
            from app.extension_sdk import ExtensionResultStatus, ObserverReceipt
            return ObserverReceipt(ExtensionResultStatus.AVAILABLE)

    class LaterObserver:
        def observe(self, _context):
            calls.append("later")

    declarations = (
        ContributionDeclaration(
            "observer.report_first",
            REPORT_COMPLETED_OBSERVER_POINT,
            ContributionKind.OBSERVER,
        ),
        ContributionDeclaration(
            "observer.report_later",
            REPORT_COMPLETED_OBSERVER_POINT,
            ContributionKind.OBSERVER,
        ),
    )
    bundles = tuple(
        _Bundle(
            ExtensionManifest(
                id=declaration.id,
                version="1.0.0",
                api_version=EXTENSION_API_VERSION,
                display_name=declaration.id,
                trust="builtin",
                requires=(REPORT_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY,),
                contributions=(declaration,),
            ),
            ExtensionContribution(
                declaration,
                FirstObserver() if index == 0 else LaterObserver(),
            ),
        )
        for index, declaration in enumerate(declarations)
    )
    runtime = build_extension_runtime(
        bundles,
        capability_decisions={
            REPORT_AGENT_PROFILE_COMPLETED_ACCESS_CAPABILITY: (
                lambda _context: Availability.available()
            )
        },
    )

    runtime.report_completed_observers.observe_application(
        _context(calls, probe=probe), event_sink=events.append
    )

    assert calls == []
    assert events[-1]["reason_code"] == "connection_lease_held"
    assert events[-1]["contribution_id"] == "observer.report_first"
