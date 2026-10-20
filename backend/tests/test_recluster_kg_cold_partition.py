"""``recluster_kg --cold-partition``: from argument parsing to the cold request,
and what the script says afterwards (design 2026-10-19 §五 S2).

The script records the request, rebuilds, then reports what actually happened:
"re-partitioned" only when a partition of this algorithm was published; when
its own rebuild was held off by a build in flight, it says the request is
recorded for the next rebuild that re-partitions the board graph -- never that
the boards were partitioned cold.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.scripts import recluster_kg
from tests.test_board_partition_wiring import _graph, _state, repo  # noqa: F401  (fixture)


class _Recorder:
    def __init__(self, states, rebuild=None):
        self.calls: list = []
        self._states = list(states)
        self._rebuild = rebuild

    def request_cold_community_partition(self, notebook_id):
        self.calls.append(("request_cold_community_partition", notebook_id))

    def rebuild_unified_kg(self, notebook_id, force=False):
        self.calls.append(("rebuild_unified_kg", notebook_id, force))
        if self._rebuild is not None:
            self._rebuild(notebook_id)
        return 7

    def community_partition_state(self, notebook_id):
        self.calls.append(("community_partition_state", notebook_id))
        return self._states.pop(0)


def _run(monkeypatch, argv, fake):
    seen: dict = {}

    @contextmanager
    def opening(settings, *, confirm_service_stopped):
        seen["confirmed"] = confirm_service_stopped
        yield fake

    monkeypatch.setattr(recluster_kg, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(recluster_kg, "open_maintenance_cli_repository", opening)
    monkeypatch.setattr(sys, "argv", ["recluster_kg", *argv])
    return recluster_kg.main(), seen


def test_the_flag_requests_cold_before_the_rebuild_and_reports_the_published_mode(
    monkeypatch, capsys,
):
    fake = _Recorder([{"version": 1, "mode": "cold"}])
    code, seen = _run(monkeypatch, ["--cold-partition", "nb-x", "--confirm-service-stopped"],
                      fake)
    assert code == 0 and seen == {"confirmed": True}
    assert fake.calls == [
        ("request_cold_community_partition", "nb-x"),
        ("rebuild_unified_kg", "nb-x", True),
        ("community_partition_state", "nb-x"),
    ]
    out = capsys.readouterr().out
    assert "cold board partition requested" in out
    assert "boards re-partitioned (cold)" in out


def test_without_the_flag_nothing_is_requested(monkeypatch, capsys):
    fake = _Recorder([])
    code, seen = _run(monkeypatch, ["nb-x"], fake)
    assert code == 0 and seen == {"confirmed": False}
    assert fake.calls == [("rebuild_unified_kg", "nb-x", True)]
    assert "cold" not in capsys.readouterr().out


@pytest.mark.parametrize("argv", [[], ["--cold-partition"], ["a", "b", "--cold-partition"]])
def test_a_missing_or_extra_notebook_id_is_a_usage_error(monkeypatch, capsys, argv):
    fake = _Recorder([])
    code, _seen = _run(monkeypatch, argv, fake)
    assert code == 2 and fake.calls == []
    assert "usage: recluster_kg" in capsys.readouterr().out


@pytest.mark.parametrize(("state", "expected"), [
    ({"version": 1, "mode": "warm"}, "boards re-partitioned (warm)"),
    ({"version": 0, "mode": "cold_fallback"}, "networkx fallback"),
    ({"version": 0, "mode": "warm"}, "the request is recorded"),
])
def test_the_report_says_what_happened(capsys, state, expected):
    recluster_kg._report_cold_partition("nb-x", state)
    out = capsys.readouterr().out
    assert expected in out
    if state["version"] == 0 and state["mode"] != "cold_fallback":
        assert "re-partitioned" not in out


def test_a_rebuild_held_off_by_a_build_in_flight_keeps_the_request(repo, monkeypatch, capsys):  # noqa: F811
    """The real gate: a build in flight holds the notebook's generation claim, so
    the script's own rebuild returns without partitioning. The version stays 0,
    the script says so, and the next rebuild starts cold."""
    nb = _graph(repo)
    lifecycle = repo._runtime.knowledge_lifecycle
    lifecycle.rebuild_communities(nb)
    with repo._runtime.database.write() as db:
        claim = repo._runtime.unified_kg.claim_derived_generation(db, nb, ttl_seconds=3600)
    assert claim is not None

    class _Real(_Recorder):
        def request_cold_community_partition(self, notebook_id):
            super().request_cold_community_partition(notebook_id)
            repo.request_cold_community_partition(notebook_id)

        def community_partition_state(self, notebook_id):
            self.calls.append(("community_partition_state", notebook_id))
            return repo.community_partition_state(notebook_id)

    fake = _Real([], rebuild=lambda notebook_id: lifecycle.rebuild_communities(
        notebook_id, force=True))
    code, _seen = _run(monkeypatch, [nb, "--cold-partition"], fake)
    assert code == 0
    out = capsys.readouterr().out
    assert "the request is recorded" in out and "re-partitioned (" not in out
    assert _state(repo, nb) == (0, "cold")
    with repo._runtime.database.write() as db:
        repo._runtime.unified_kg.release_derived_claim(db, nb, claim["generation"])
    lifecycle.rebuild_communities(nb, force=True)
    assert repo.community_partition_state(nb) == {"version": 1, "mode": "cold"}
