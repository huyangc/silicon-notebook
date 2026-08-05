from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.repositories.postgres.maintenance import PostgresMaintenanceAdapter
from app.repositories.sqlite.maintenance import SQLiteMaintenanceAdapter


class _Artifacts:
    def __init__(self) -> None:
        self.main = {"version": [4, 2], "n_nodes": 17}
        self.partition = {
            "format_version": 1,
            "parent_version": [4, 2],
            "published_sources": 3,
            "unavailable_sources": 0,
        }
        self.corrupt = False

    def scale_dir(self, _notebook_id):
        return "main"

    def source_partition_dir(self, _notebook_id):
        return "partition"

    def read_manifest(self, directory):
        if self.corrupt:
            raise json.JSONDecodeError("invalid", "", 0)
        return self.main if directory == "main" else self.partition


def _adapters(artifacts):
    runtime = SimpleNamespace(
        scale_artifacts=SimpleNamespace(
            artifacts=artifacts,
            version=lambda _notebook_id: [4, 2],
        )
    )
    return (
        SQLiteMaintenanceAdapter(runtime, retrieval=lambda: None),
        PostgresMaintenanceAdapter(runtime),
    )


@pytest.mark.parametrize("adapter_index", [0, 1])
def test_artifact_status_is_a_cheap_version_and_count_probe(adapter_index):
    artifacts = _Artifacts()
    adapter = _adapters(artifacts)[adapter_index]

    assert adapter.selected_source_graph_artifact_status("nb-1") == {
        "ready": True,
        "n_nodes": 17,
        "published_sources": 3,
        "unavailable_sources": 0,
    }

    artifacts.partition["parent_version"] = [4, 1]
    assert adapter.selected_source_graph_artifact_status("nb-1")["ready"] is False


@pytest.mark.parametrize("adapter_index", [0, 1])
def test_artifact_status_fails_closed_on_corrupt_manifest(adapter_index):
    artifacts = _Artifacts()
    artifacts.corrupt = True
    adapter = _adapters(artifacts)[adapter_index]

    assert adapter.selected_source_graph_artifact_status("nb-1") == {
        "ready": False,
        "n_nodes": 0,
        "published_sources": 0,
        "unavailable_sources": 0,
    }
