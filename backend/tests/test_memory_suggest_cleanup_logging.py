"""A committed access change survives content-free suggestion cleanup failures."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.services.notebook_sharing import NotebookSharingService


@pytest.mark.parametrize("operation", ["remove_member", "forget_member_state"])
@pytest.mark.parametrize("failure", [RuntimeError, ValueError])
def test_suggestion_cleanup_is_fail_open_and_logs_only_the_exception_class(operation, failure, caplog):
    calls = []
    notebook, owner = "nb-private-cleanup", "user-private-cleanup"

    def fail_suggestion_cleanup(nb, owners):
        calls.append(("suggestion", nb, owners))
        raise failure("SENSITIVE-SYNTHETIC-SQL-PARAMETERS")

    service = NotebookSharingService(
        store=SimpleNamespace(remove_member=lambda nb, user: calls.append(("removed", nb, user))),
        copies=None, catalog=None, summaries=None, database=None, copy_stats=lambda _nb: {},
        memory_suggest=SimpleNamespace(clear_job_rows=fail_suggestion_cleanup),
        profiles=SimpleNamespace(
            clear_job_rows=lambda nb, owners: calls.append(("profile_jobs", nb, owners)),
            clear_all=lambda nb, owners: calls.append(("profile_blocks", nb, owners)),
        ),
        observations=SimpleNamespace(
            clear_observations=lambda nb, owners: calls.append(("observations", nb, owners)),
        ),
    )
    with caplog.at_level(logging.WARNING, logger="silicon_notebook.sharing"):
        getattr(service, operation)(notebook, owner)

    expected = [("removed", notebook, owner)] if operation == "remove_member" else []
    expected += [(kind, notebook, [owner]) for kind in
        ("suggestion", "profile_jobs", "profile_blocks", "observations")]
    assert calls == expected
    assert [record.getMessage() for record in caplog.records] == [
        f"memory suggestion job cleanup failed: {failure.__name__}",
    ]
    assert all(record.exc_info is None and record.stack_info is None for record in caplog.records)
    assert "SENSITIVE-SYNTHETIC" not in caplog.text
    assert notebook not in caplog.text
    assert owner not in caplog.text
