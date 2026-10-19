"""The workbook lane in a peer (global) run: one federated catalog, one plan.

``fangan_todo`` 「全局（对等）模式的表格分析臂只覆盖名义 active」 — user ruling
2026-10-19: federate, with the workbook total capped.  Every participant
library's workbooks are merged into ONE catalog and planned ONCE; the catalog
is filled library by library in rotation under the same byte budget a single
library's catalog already has, so neither the planner call count nor the
number of manifests read grows with the number of libraries; the participant
set is read through the chunk lane's own bounded entry
(``chunk_federation.federated_library_sources``), never through the
authorization-grade ``ask_engine_participant_notebooks`` seat.

The single-library lane is pinned byte for byte (planner prompt digest, the
exact manifest reads, the result) by ``test_single_library_lane_is_byte_identical``.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from openpyxl import Workbook

from app.core.config import Settings
from app.repositories.analysis_artifacts import AnalysisArtifactStore
from app.services.ask_service import AskService
from app.services.spreadsheet_analysis import SpreadsheetAnalysisService


_NOW = "2026-10-19T00:00:00+00:00"


class _EventLog:
    def __init__(self) -> None:
        self.events: list = []
        self.logger = SimpleNamespace(
            exception=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
        )

    def emit(self, event) -> None:
        self.events.append(event)


class _CountingArtifacts(AnalysisArtifactStore):
    """The real store, with every manifest read and directory listing counted."""

    def __init__(self, root: Path) -> None:
        super().__init__(root, retention_days=30)
        self.loads: list[tuple[str, str]] = []
        self.listings: list[str] = []

    def load_spreadsheet_manifest(self, notebook_id, source_id):
        self.loads.append((notebook_id, source_id))
        return super().load_spreadsheet_manifest(notebook_id, source_id)

    def spreadsheet_manifest_ids(self, notebook_id):
        self.listings.append(notebook_id)
        return super().spreadsheet_manifest_ids(notebook_id)


class _Planner:
    """A configured ``reasoning_agent`` that records every planner prompt."""

    configured = True

    def __init__(self, plan: dict | None = None) -> None:
        self.plan = plan or {}
        self.prompts: list[str] = []

    def chat_json(self, messages, *args, **kwargs) -> str:
        self.prompts.append(messages[0]["content"])
        return json.dumps(self.plan)


def _settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        _env_file=None,
        storage_dir=str(tmp_path / "storage"),
        event_log_enabled=False,
        llm_log_enabled=False,
        **overrides,
    )


def _service(tmp_path: Path, **overrides):
    settings = _settings(tmp_path, **overrides)
    artifacts = _CountingArtifacts(Path(settings.storage_dir))
    return SpreadsheetAnalysisService(
        artifacts=artifacts, settings=settings, event_log=_EventLog(),
        now=lambda: _NOW,
    ), artifacts


def _compile(service, tmp_path: Path, notebook_id: str, source_id: str, *,
             title: str = "", header=("Region", "Amount"), rows=(("East", 10),)):
    path = tmp_path / f"{notebook_id}-{source_id}.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sheet"
    sheet.append(list(header))
    for row in rows:
        sheet.append(list(row))
    workbook.save(path)
    source = SimpleNamespace(
        id=source_id, notebook_id=notebook_id, title=title or source_id,
        type="xlsx", file_name=f"{source_id}.xlsx", file_path=str(path),
        file_hash="h", source_url="",
    )
    assert service.compile_source(
        source, notebook_name=notebook_id, owner_id="owner", row_element_ids={},
    ) is True


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _NoSeat:
    """A retrieval double whose federated seat must never be read."""

    def workbook_libraries(self, notebook_id):  # pragma: no cover - must not run
        raise AssertionError("a single-library run must not read the peer seat")


def _ask(analysis, planner, *, participants, visible, tiers=None, retrieval=None):
    """A bare ``AskService`` carrying only the seats the workbook lane reads."""
    ask = object.__new__(AskService)
    ask.spreadsheet_analysis = analysis
    ask.ask_engine_hidden_sources = lambda notebook_id, user_id: ()
    ask.ask_engine_participant_notebooks = lambda notebook_id: participants
    ask.ask_engine_visible_sources = lambda notebook_id: visible.get(notebook_id, ())
    ask.evidence_context = SimpleNamespace(tier_map=lambda ids: {
        nb: (tiers or {}).get(nb, "personal") for nb in ids
    })
    ask.model_clients = SimpleNamespace(chat=lambda name: planner)
    ask.event_log = SimpleNamespace(logger=SimpleNamespace(warning=lambda *a: None))
    ask.retrieval = retrieval if retrieval is not None else _NoSeat()
    return ask


def _run_lane(ask, question, notebook_id="nb-1", scope=None):
    prepared = SimpleNamespace(notebook_id=notebook_id, user_id="u",
                               research_question=question)
    runtime = SimpleNamespace(scope=scope, cancellation=None, trace_sink=None)
    trace: list = []
    results = ask._spreadsheet_reasoning_results(prepared, runtime, trace)
    return results, trace


# ---------------------------------------------------------------------------
# Single-library lane: byte for byte
# ---------------------------------------------------------------------------

# Captured on the pre-federation base (ce7d6d81d) with this exact fixture.
_SINGLE_PROMPT_SHA = "99fd3f5daa8783179c4cf79590170b80af52171a6fbef3720683f401658198a0"
_SINGLE_RESULT_SHA = "d00bde19954f65d34e5647c7b095921b38087be96417bd1ebc1ac1651a56af22"


def test_single_library_lane_is_byte_identical(tmp_path):
    analysis, artifacts = _service(tmp_path)
    _compile(analysis, tmp_path, "nb-1", "src-own", title="本库报价",
             header=("Item", "Price"), rows=(("bolt", 3), ("nut", 1)))
    _compile(analysis, tmp_path, "base-1", "src-base", title="参考报价",
             header=("Item", "Price"), rows=(("bolt", 4),))
    planner = _Planner({"source_id": "src-base", "sheet": "Sheet",
                        "operation": "top", "sort_by": "Price", "order": "desc"})
    ask = _ask(analysis, planner, participants=("base-1",), visible={
        "nb-1": ("doc-1", "src-own"), "base-1": ("src-base", "doc-2"),
    }, tiers={"base-1": "base"})

    results, trace = _run_lane(ask, "分析这个 Excel 报价表里价格最高的项目")

    assert len(planner.prompts) == 1
    assert _digest(planner.prompts[0]) == _SINGLE_PROMPT_SHA, planner.prompts[0]
    assert artifacts.loads == [
        ("nb-1", "doc-1"), ("nb-1", "src-own"),
        ("base-1", "src-base"), ("base-1", "doc-2"),
    ]
    [result] = results
    dumped = json.dumps(result.model_dump(mode="json"), ensure_ascii=False,
                        sort_keys=True)
    assert _digest(dumped) == _SINGLE_RESULT_SHA, dumped
    assert (result.notebook_id, result.tier) == ("base-1", "base")
    [step] = trace
    assert step.detail == {
        "source_id": "src-base", "sheet": "Sheet", "range": step.detail["range"],
        "operation": "top", "scanned_rows": 1, "returned_rows": 1, "complete": True,
    }
