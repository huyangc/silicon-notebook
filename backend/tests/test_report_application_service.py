from __future__ import annotations

import pytest


class _Catalog:
    def __init__(self, calls: list, *, error: Exception | None = None) -> None:
        self.calls = calls
        self.error = error

    def get_notebook(self, notebook_id: str):
        self.calls.append(("guard", notebook_id))
        if self.error is not None:
            raise self.error
        return object()


class _Reports:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    def create_report(self, notebook_id: str, question: str, depth: int = 2) -> str:
        self.calls.append(("store", notebook_id, question, depth))
        return "rep-created"


def test_create_report_guards_before_writing():
    from app.services.report_application import ReportApplicationService

    calls = []
    service = ReportApplicationService(_Catalog(calls), _Reports(calls))

    assert service.create_report("nb-1", "why?", 3) == "rep-created"
    assert calls == [
        ("guard", "nb-1"),
        ("store", "nb-1", "why?", 3),
    ]


def test_create_report_propagates_keyerror_without_store_call():
    from app.services.report_application import ReportApplicationService

    calls = []
    missing = KeyError("nb-missing")
    service = ReportApplicationService(
        _Catalog(calls, error=missing), _Reports(calls)
    )

    with pytest.raises(KeyError) as captured:
        service.create_report("nb-missing", "why?")

    assert captured.value is missing
    assert calls == [("guard", "nb-missing")]
