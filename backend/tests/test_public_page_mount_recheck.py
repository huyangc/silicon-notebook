"""Public pages re-check the libraries they draw on, on SQLite (E7-5: D-3, D-2).

The scenarios live in ``public_page_mount_recheck_cases.py`` and run here and,
unchanged, on PostgreSQL in ``postgres/test_public_page_mount_recheck_pg.py``.
This file adds the pure half: which libraries a conversation names, and the
check itself.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.services.public_share_recheck import (
    conversation_library_ids,
    mounts_still_effective,
)
from tests.public_page_mount_recheck_cases import CASES
from tests.report_share_disclosure_cases import build_world


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.api import deps
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    return build_world(TestClient(create_app()), monkeypatch)


@pytest.mark.parametrize("case", sorted(CASES))
def test_public_page_mount_recheck_scenario(world, case):
    CASES[case](world)


def test_a_conversation_names_the_libraries_on_its_anchors_and_citations():
    turns = [
        {"payload": {"anchors": [{"notebook_id": "nb-b"}, {"key": "k2"}],
                     "citations": [{"notebook_id": "nb-c"}]}},
        {"payload": "not a payload"},
        "not a turn",
    ]
    assert conversation_library_ids(turns) == {"nb-b", "nb-c"}


def test_the_check_reads_nothing_without_another_library_and_fails_closed():
    def never(_notebook_id):
        raise AssertionError("no other library: nothing to read")

    assert mounts_still_effective("nb-a", "u", {"nb-a", ""}, never)
    assert not mounts_still_effective("nb-a", "", {"nb-b"}, lambda _nb: ["nb-a", "nb-b"])
    assert mounts_still_effective("nb-a", "u", {"nb-b"}, lambda _nb: ["nb-a", "nb-b"])
    assert not mounts_still_effective("nb-a", "u", {"nb-b", "nb-c"}, lambda _nb: ["nb-a", "nb-b"])
