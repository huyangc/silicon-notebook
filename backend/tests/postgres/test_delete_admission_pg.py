from __future__ import annotations

import pytest

from tests.delete_admission_cases import (
    assert_candidate_admission,
    assert_existing_disabled_mount_is_preserved,
    assert_root_admission,
    assert_source_admission,
)
from tests.notebook_delete_grace_cases import build_world
from tests.postgres.test_notebook_delete_grace_pg import repo as repo

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_notebook_delete_grace"),
]


@pytest.mark.parametrize("status", ["pending_delete", "deleting"])
@pytest.mark.parametrize("joined", [False, True])
def test_late_source_write_rechecks_lifecycle(repo, monkeypatch, status, joined):
    assert_source_admission(build_world(repo, postgres=True, monkeypatch=monkeypatch), status, joined=joined)


@pytest.mark.parametrize("status", ["pending_delete", "deleting"])
def test_candidate_write_rechecks_after_service_preflight(repo, monkeypatch, status):
    assert_candidate_admission(build_world(repo, postgres=True, monkeypatch=monkeypatch), monkeypatch, status)


@pytest.mark.parametrize("status", ["pending_delete", "deleting"])
@pytest.mark.parametrize("root", ["report", "mount", "mount_target", "share", "knowhow", "knowhow_import"])
def test_new_root_content_rechecks_lifecycle(repo, monkeypatch, status, root):
    assert_root_admission(build_world(repo, postgres=True, monkeypatch=monkeypatch), status, root)


@pytest.mark.parametrize("status", ["pending_delete", "deleting"])
def test_existing_disabled_mount_can_be_preserved(repo, monkeypatch, status):
    assert_existing_disabled_mount_is_preserved(
        build_world(repo, postgres=True, monkeypatch=monkeypatch), status,
    )
