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
    # The image case needs the deployment to serve answer images.
    monkeypatch.setenv("MINERU_RETURN_IMAGES", "true")
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


def test_report_ownership_is_read_only_for_mounted_or_unmarked_citations():
    from app.services.public_share_recheck import (
        report_library_ids,
        report_references_needing_owner,
        report_unresolved_object_ids,
    )

    local = {"source_id": "s-local", "from_reference_library": False}
    legacy = {"source_id": "s-legacy"}
    mounted = {"source_id": "s-gone", "object_id": "ko-1", "from_reference_library": True}
    wanted = report_references_needing_owner([local, legacy, mounted, "junk"])
    assert wanted == [legacy, mounted]
    assert report_unresolved_object_ids(wanted, {"s-legacy": "nb-a"}) == ["ko-1"]
    assert report_library_ids(wanted, {"s-legacy": "nb-a"}, {"ko-1": "nb-b"}) == {"nb-a", "nb-b"}
    # A marked citation naming no library fails closed; a legacy one is not counted.
    assert report_library_ids(wanted, {"s-legacy": "nb-a"}, {}) is None
    assert report_library_ids([legacy], {}, {}) == set()


def test_a_report_citation_stores_its_mounted_library_only():
    """The engine's stored citation fields: a mounted library is named (so the
    page can still re-check it after the cited source is deleted); local
    evidence names none, keeping a local report's stored bytes."""
    from app.services.report_engine import reference_library_fields

    assert reference_library_fields("nb-b", "nb-a") == {
        "from_reference_library": True, "notebook_id": "nb-b"}
    assert reference_library_fields("nb-a", "nb-a") == {"from_reference_library": False}


def test_a_stored_library_is_used_before_any_read():
    from app.services.public_share_recheck import (
        report_library_ids,
        report_source_ids,
        report_unresolved_object_ids,
    )

    named = {"source_id": "s-gone", "object_id": "ko-gone",
             "from_reference_library": True, "notebook_id": "nb-b"}
    assert report_source_ids([named]) == []
    assert report_unresolved_object_ids([named], {}) == []
    assert report_library_ids([named], {}, {}) == {"nb-b"}


def test_the_check_reads_nothing_without_another_library_and_fails_closed():
    def never(_notebook_id, *, viewer_id):
        raise AssertionError("no other library: nothing to read")

    viewers: list[str] = []

    def effective(_notebook_id, *, viewer_id):
        viewers.append(viewer_id)
        return ["nb-a", "nb-b"]

    assert mounts_still_effective("nb-a", "u", {"nb-a", ""}, never)
    assert not mounts_still_effective("nb-a", "", {"nb-b"}, effective)
    assert viewers == [], "an empty creator fails closed without reading"
    assert mounts_still_effective("nb-a", "u", {"nb-b"}, effective)
    assert not mounts_still_effective("nb-a", "u", {"nb-b", "nb-c"}, effective)
    # M3: the participant set is read AS the share's creator (the anonymous page
    # binds no request user), never as nobody.
    assert viewers == ["u", "u"]
