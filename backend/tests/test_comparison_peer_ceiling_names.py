"""Rule R3 of the comparison-peer name exit on SQLite: under a per-library
source ceiling a peer is named only from in-ceiling members.

The scenarios and the world are backend-neutral
(``tests/comparison_peer_names_cases.py``); the PostgreSQL twin is
``tests/postgres/test_comparison_peer_ceiling_names_pg.py``. The SQL source
gate that decides WHICH peers survive is pinned by
``tests/test_comparison_peer_ceiling.py``.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests import comparison_peer_names_cases as cases
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    repository = SQLiteRepository(Settings())
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    return cases.build_world(repository, postgres=False)


@pytest.mark.parametrize("scenario", cases.SCENARIOS, ids=lambda s: s.__name__)
def test_comparison_peer_names(world, monkeypatch, scenario):
    scenario(world, monkeypatch)


@pytest.mark.parametrize("path", cases.PATHS)
@pytest.mark.parametrize("scenario", cases.PATH_SCENARIOS, ids=lambda s: s.__name__)
def test_comparison_peer_names_per_path(world, monkeypatch, scenario, path):
    scenario(world, monkeypatch, path)


class _HubStore:
    """A hub cluster of ``total`` members in member-id order, all owned by a
    source outside the ceiling except (optionally) the one at ``inside``; it
    records the page size of every read. Pure Python: the paging policy
    under test lives in the service, the store SQL is pinned above."""

    def __init__(self, total: int, inside: int | None = None) -> None:
        self.total, self.inside, self.limits = total, inside, []

    def comparison_peer_members(self, notebook_id, canonical_ids, *, limit, after=""):
        self.limits.append(limit)
        start = int(after.rsplit("-", 1)[1]) + 1 if after else 0
        return [
            {"canonical_id": canonical_ids[0], "member_object_id": f"ko-{index:06d}",
             "source_id": "s-in" if index == self.inside else "s-out",
             "member_name": "Hub" if index == self.inside else "Hidden Hub"}
            for index in range(start, min(start + limit, self.total))
        ]


@pytest.mark.parametrize("inside", [None, 3000], ids=["no-member", "member-deep"])
def test_member_paging_window_is_capped(inside):
    """``_scan_peer_members`` doubles its page while a full page holds no
    in-ceiling member, but never past ``_PEER_MEMBER_PAGE_MAX`` rows; it still
    reads to the member it needs or to the end of the cluster."""
    from app.services import communities

    store = _HubStore(5000, inside)
    service = communities.CommunityQueryService(
        notebooks=None, unified_kg=store, event_log=None)
    name = service._scan_peer_members(
        "nb", "K-hub", "Hidden Hub", frozenset({"s-in"}),
        after="ko-000007", window=2 * communities._PEER_MEMBER_WINDOW)
    cap = communities._PEER_MEMBER_PAGE_MAX
    assert cap == 512
    assert store.limits[:6] == [16, 32, 64, 128, 256, 512]
    assert max(store.limits) == cap and len(store.limits) > 6
    read = sum(store.limits[:-1])
    if inside is None:
        assert name is None and 8 + read < 5000 <= 8 + sum(store.limits)
    else:
        assert name == "Hub" and 8 + read <= inside < 8 + sum(store.limits)
