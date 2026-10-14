"""Shared pieces of the sync test files' seeded world templates.

``test_sync_incremental_import.py``, ``test_sync_import.py``,
``test_sync_export.py`` and ``test_sync_incremental.py`` build their seeded
worlds once per process through ``tests/world_templates.py`` (``pinned``
layout: uploaded sources store absolute ``file_path``s, and a package's report
names its own directory). A template payload has to be pure data, so the
``ExportReport`` / ``ImportReport`` a fixture used to hold as a live object is
carried as a dict and rebuilt per test. The conversion proves itself at build
time: the rebuilt report must equal the original field for field AND keep
every field's type, so a report field added later fails the build loudly
instead of reaching a test as the wrong shape.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from types import MappingProxyType
from typing import Any

from app.migration.sync.export import ExportReport
from app.migration.sync.import_ import (
    ImportReport,
    SkippedRow,
    TableOutcome,
    UserMappingResult,
)


def _to_data(value: Any) -> Any:
    if isinstance(value, Path):
        return {"$path": str(value)}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            "$dataclass": type(value).__name__,
            "fields": {
                field.name: _to_data(getattr(value, field.name))
                for field in dataclasses.fields(value)
            },
        }
    if isinstance(value, MappingProxyType):
        return {"$mappingproxy": {key: _to_data(item) for key, item in value.items()}}
    if isinstance(value, dict):
        return {"$dict": {key: _to_data(item) for key, item in value.items()}}
    if isinstance(value, tuple):
        return {"$tuple": [_to_data(item) for item in value]}
    if isinstance(value, list):
        return {"$list": [_to_data(item) for item in value]}
    if value is None or type(value) in (bool, int, float, str):
        return value
    raise TypeError(f"{type(value).__name__} has no pure-data form here")


_DATACLASSES = {
    cls.__name__: cls
    for cls in (ExportReport, ImportReport, SkippedRow, TableOutcome, UserMappingResult)
}


def _from_data(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    if "$path" in data:
        return Path(data["$path"])
    if "$dataclass" in data:
        cls = _DATACLASSES[data["$dataclass"]]
        return cls(**{key: _from_data(item) for key, item in data["fields"].items()})
    if "$mappingproxy" in data:
        return MappingProxyType(
            {key: _from_data(item) for key, item in data["$mappingproxy"].items()}
        )
    if "$dict" in data:
        return {key: _from_data(item) for key, item in data["$dict"].items()}
    if "$tuple" in data:
        return tuple(_from_data(item) for item in data["$tuple"])
    if "$list" in data:
        return [_from_data(item) for item in data["$list"]]
    raise ValueError(f"not a report payload: {sorted(data)}")


def _same_shape(left: Any, right: Any, where: str) -> None:
    if type(left) is not type(right):
        raise AssertionError(
            f"{where}: rebuilt as {type(right).__name__}, was {type(left).__name__}"
        )
    if dataclasses.is_dataclass(left):
        for field in dataclasses.fields(left):
            _same_shape(
                getattr(left, field.name),
                getattr(right, field.name),
                f"{where}.{field.name}",
            )
    elif isinstance(left, (dict, MappingProxyType)):
        for key in left:
            _same_shape(left[key], right[key], f"{where}[{key!r}]")
    elif isinstance(left, (list, tuple)):
        for index, (one, other) in enumerate(zip(left, right)):
            _same_shape(one, other, f"{where}[{index}]")


def report_data(report: ExportReport | ImportReport) -> dict:
    """Pure-data form of an export/import report, verified to rebuild exactly."""
    data = _to_data(report)
    rebuilt = report_from_data(data)
    if rebuilt != report:
        raise AssertionError(f"{type(report).__name__} does not survive its payload form")
    _same_shape(report, rebuilt, type(report).__name__)
    return data


def report_from_data(data: dict) -> Any:
    """A fresh report object from ``report_data``'s output (one per test)."""
    return _from_data(data)
