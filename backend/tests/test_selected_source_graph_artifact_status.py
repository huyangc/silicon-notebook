from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.repositories.postgres.maintenance import PostgresMaintenanceAdapter
from app.repositories.sqlite.maintenance import SQLiteMaintenanceAdapter
from app.services.kg.source_partition_index import SOURCE_PARTITION_FORMAT_VERSION


class _Artifacts:
    def __init__(self) -> None:
        self.main = {"version": [4, 2], "n_nodes": 17}
        self.partition = {
            "format_version": SOURCE_PARTITION_FORMAT_VERSION,
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

    artifacts.partition["parent_version"] = [4, 2]
    artifacts.partition["format_version"] = SOURCE_PARTITION_FORMAT_VERSION + 1
    assert adapter.selected_source_graph_artifact_status("nb-1")["ready"] is False


@pytest.mark.parametrize("adapter_index", [0, 1])
def test_artifact_status_refuses_a_same_version_pair_from_two_builds(adapter_index):
    """P1, codex PR#643 R26. Every version here agrees — a same-version
    republish is supported — and the two roots were still produced by
    different builds, which is what a publish interrupted between them
    leaves. The reader refuses that pair, so this probe must not answer
    ``ready``: it is what ``prepare_selected_source_graph`` consults to decide
    whether a companion needs rebuilding, and a false ``ready`` there means
    the repair step skips the very notebook that needs it.

    Mutation anchor: drop the ``build_generation_mismatch`` term from either
    adapter's ``ready`` expression and the mixed pair below reports ready.
    """
    artifacts = _Artifacts()
    adapter = _adapters(artifacts)[adapter_index]
    artifacts.main["build_id"] = "a" * 32
    artifacts.partition["parent_build_id"] = "b" * 32

    assert adapter.selected_source_graph_artifact_status("nb-1")["ready"] is False

    artifacts.partition["parent_build_id"] = "a" * 32
    assert adapter.selected_source_graph_artifact_status("nb-1")["ready"] is True


@pytest.mark.parametrize("adapter_index", [0, 1])
@pytest.mark.parametrize(
    "main_build_id, companion_build_id",
    [(None, None), ("a" * 32, None), (None, "b" * 32)],
    ids=["neither-side", "main-only", "companion-only"],
)
def test_artifact_status_keeps_pairing_legacy_roots_on_version(
    adapter_index, main_build_id, companion_build_id
):
    """older-index-stays-valid, mirrored here so this probe cannot start
    reporting every pre-existing companion as un-ready (which would send
    ``prepare_selected_source_graph`` off to rebuild all of them)."""
    artifacts = _Artifacts()
    adapter = _adapters(artifacts)[adapter_index]
    if main_build_id is not None:
        artifacts.main["build_id"] = main_build_id
    if companion_build_id is not None:
        artifacts.partition["parent_build_id"] = companion_build_id

    assert adapter.selected_source_graph_artifact_status("nb-1")["ready"] is True


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
