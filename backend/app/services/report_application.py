from __future__ import annotations

from typing import Protocol

from app.models.ask import StoredSubmittedVia


class NotebookGuard(Protocol):
    def get_notebook(self, notebook_id: str): ...


class ReportCreator(Protocol):
    def create_report(
        self,
        notebook_id: str,
        question: str,
        depth: int = 2,
        *,
        submitted_via: StoredSubmittedVia = "",
    ) -> str: ...


class ReportApplicationService:
    """Application-level report creation guard over row persistence."""

    def __init__(self, notebooks: NotebookGuard, reports: ReportCreator) -> None:
        self.notebooks = notebooks
        self.reports = reports

    def create_report(
        self,
        notebook_id: str,
        question: str,
        depth: int = 2,
        *,
        submitted_via: StoredSubmittedVia = "",
    ) -> str:
        self.notebooks.get_notebook(notebook_id)
        return self.reports.create_report(
            notebook_id, question, depth, submitted_via=submitted_via
        )


__all__ = ["ReportApplicationService"]
