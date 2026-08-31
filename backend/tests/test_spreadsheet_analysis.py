from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from app.core.config import Settings
from app.repositories.analysis_artifacts import AnalysisArtifactStore
from app.services.cancellation import AskCancelled
from app.services.spreadsheet_analysis import SpreadsheetAnalysisService


class _EventLog:
    def __init__(self) -> None:
        self.events = []
        self.logger = SimpleNamespace(
            exception=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
        )

    def emit(self, event) -> None:
        self.events.append(event)


class _OfflinePlanner:
    configured = False


class _AggregatePlanner:
    configured = True

    def chat_json(self, *args, **kwargs) -> str:
        return json.dumps({
            "source_id": "src-1",
            "sheet": "Sales",
            "operation": "aggregate",
            "aggregation": "sum",
            "measure": "Amount",
            "group_by": "Region",
            "filters": [],
        })


class _CancelledPlanner:
    configured = True

    def chat_json(self, *args, **kwargs) -> str:
        raise AskCancelled("cancelled")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        storage_dir=str(tmp_path / "storage"),
        event_log_enabled=False,
        llm_log_enabled=False,
    )


def _source(path: Path):
    return SimpleNamespace(
        id="src-1",
        notebook_id="nb-1",
        title="Sales workbook",
        type="xlsx",
        file_name="sales.xlsx",
        file_path=str(path),
        file_hash="abc",
        source_url="",
    )


def _workbook(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sales"
    sheet.append(["Region", "Amount", "Owner"])
    sheet.append(["East", 10, "A"])
    sheet.append(["West", 20, "B"])
    sheet.append(["East", 40, "C"])
    workbook.save(path)


def test_compile_and_offline_profile_are_bounded_and_citable(tmp_path):
    path = tmp_path / "sales.xlsx"
    _workbook(path)
    settings = _settings(tmp_path)
    artifacts = AnalysisArtifactStore(
        Path(settings.storage_dir), retention_days=30
    )
    service = SpreadsheetAnalysisService(
        artifacts=artifacts,
        settings=settings,
        event_log=_EventLog(),
        now=lambda: "2026-08-31T01:00:00+00:00",
    )

    assert service.compile_source(
        _source(path),
        notebook_name="Notebook",
        owner_id="user-1",
        row_element_ids={
            ("Sales", 1): "el-1",
            ("Sales", 2): "el-2",
            ("Sales", 3): "el-3",
            ("Sales", 4): "el-4",
        },
    ) is True

    results, trace = service.analyze(
        notebook_id="nb-1",
        source_ids=("src-1",),
        question="分析这个 Excel 的数据质量和缺失情况",
        planner_client=_OfflinePlanner(),
    )
    assert trace is not None
    assert trace.step_type == "spreadsheet"
    [result] = results
    assert result.kind == "spreadsheet"
    assert result.operation == "profile"
    assert result.coverage.scanned_rows == 3
    assert [column.name for column in result.columns][:3] == ["列", "类型", "非空"]
    assert result.rows[0].citation is not None
    assert result.rows[0].citation.element_id == "el-2"


def test_planner_selects_whitelisted_grouped_aggregate(tmp_path):
    path = tmp_path / "sales.xlsx"
    _workbook(path)
    settings = _settings(tmp_path)
    service = SpreadsheetAnalysisService(
        artifacts=AnalysisArtifactStore(Path(settings.storage_dir), retention_days=30),
        settings=settings,
        event_log=_EventLog(),
        now=lambda: "2026-08-31T01:00:00+00:00",
    )
    assert service.compile_source(
        _source(path), notebook_name="Notebook", owner_id="user-1",
        row_element_ids={("Sales", 2): "el-2"},
    )

    results, _ = service.analyze(
        notebook_id="nb-1",
        source_ids=("src-1",),
        question="按 Region 汇总 Amount 的总和",
        planner_client=_AggregatePlanner(),
    )
    [result] = results
    assert result.operation == "aggregate"
    assert result.rows[0].cells == {"Region": "East", "Amount · sum": "50"}
    assert result.rows[1].cells == {"Region": "West", "Amount · sum": "20"}


def test_planner_cancellation_is_not_downgraded_to_local_profile(tmp_path):
    path = tmp_path / "sales.xlsx"
    _workbook(path)
    settings = _settings(tmp_path)
    service = SpreadsheetAnalysisService(
        artifacts=AnalysisArtifactStore(Path(settings.storage_dir), retention_days=30),
        settings=settings,
        event_log=_EventLog(),
        now=lambda: "2026-08-31T01:00:00+00:00",
    )
    assert service.compile_source(
        _source(path), notebook_name="Notebook", owner_id="user-1",
        row_element_ids={},
    )

    with pytest.raises(AskCancelled):
        service.analyze(
            notebook_id="nb-1",
            source_ids=("src-1",),
            question="分析这个 Excel",
            planner_client=_CancelledPlanner(),
        )


def test_challenging_workbook_is_archived_instead_of_silently_misanalysed(tmp_path):
    path = tmp_path / "sales.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sales"
    sheet.append(["Region", "Amount"])
    sheet.append(["East", 10])
    sheet.append([])
    sheet.append(["Owner", "Quota"])
    sheet.append(["A", 20])
    workbook.save(path)
    settings = _settings(tmp_path)
    artifacts = AnalysisArtifactStore(Path(settings.storage_dir), retention_days=30)
    service = SpreadsheetAnalysisService(
        artifacts=artifacts,
        settings=settings,
        event_log=_EventLog(),
        now=lambda: "2026-08-31T01:00:00+00:00",
    )

    assert service.compile_source(
        _source(path), notebook_name="Notebook", owner_id="user-1",
        row_element_ids={},
    ) is False
    [issue] = artifacts.list_issues(status="open")
    assert issue["code"] == "SPREADSHEET_MULTIPLE_REGIONS"
    assert issue["artifact_available"] is True
    assert artifacts.load_spreadsheet_manifest("nb-1", "src-1") is None


def test_oversized_cell_is_rejected_without_truncation(tmp_path):
    path = tmp_path / "sales.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sales"
    sheet.append(["Region", "Notes"])
    sheet.append(["East", "x" * 300])
    workbook.save(path)
    settings = Settings(
        _env_file=None,
        storage_dir=str(tmp_path / "storage"),
        event_log_enabled=False,
        llm_log_enabled=False,
        spreadsheet_analysis_max_cell_chars=256,
    )
    artifacts = AnalysisArtifactStore(Path(settings.storage_dir), retention_days=30)
    service = SpreadsheetAnalysisService(
        artifacts=artifacts,
        settings=settings,
        event_log=_EventLog(),
        now=lambda: "2026-08-31T01:00:00+00:00",
    )

    assert service.compile_source(
        _source(path), notebook_name="Notebook", owner_id="user-1",
        row_element_ids={},
    ) is False
    [issue] = artifacts.list_issues(status="open")
    assert issue["code"] == "SPREADSHEET_CELL_TOO_LONG"


def test_issue_archive_resolves_redacts_and_expires(tmp_path):
    store = AnalysisArtifactStore(tmp_path, retention_days=2)
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(b"private")
    occurred = "2026-08-29T00:00:00+00:00"
    store.record_issue(
        notebook_id="nb-1",
        notebook_name="Private notebook",
        owner_id="user-1",
        source_id="src-1",
        source_title="Private title",
        file_name="private.pdf",
        source_type="pdf",
        category="source_parse",
        code="SOURCE_PARSE_FAILED",
        summary="safe",
        occurred_at=occurred,
        source_path=str(source_path),
    )
    [open_issue] = store.list_issues(
        status="open", now=datetime(2026, 8, 30, tzinfo=timezone.utc)
    )
    assert open_issue["artifact_available"] is True

    store.resolve_issue(
        "nb-1", "src-1", "source_parse",
        resolved_at="2026-08-30T12:00:00+00:00",
    )
    [resolved] = store.list_issues(
        status="resolved", now=datetime(2026, 8, 30, 13, tzinfo=timezone.utc)
    )
    assert resolved["artifact_available"] is False

    store.redact_source(
        "nb-1", "src-1", occurred_at="2026-08-30T14:00:00+00:00"
    )
    [redacted] = store.list_issues(
        now=datetime(2026, 8, 30, 15, tzinfo=timezone.utc)
    )
    assert redacted["source_deleted"] is True
    assert redacted["source_title"] == ""
    assert redacted["file_name"] == ""

    assert store.list_issues(
        now=datetime(2026, 9, 1, 1, tzinfo=timezone.utc)
    ) == []
