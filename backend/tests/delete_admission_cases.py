"""Content writes must recheck lifecycle after their earlier service preflight."""
from __future__ import annotations

import pytest


def assert_source_admission(world, status: str, *, joined: bool) -> None:
    world.sql.write("UPDATE notebooks SET status=? WHERE id=?", (status, world.owner_target))
    store = world.runtime.source_store
    arguments = dict(
        source_id="late-source", notebook_id=world.owner_target, title="late.md",
        source_type="markdown", status="uploaded", parse_status="uploaded",
        file_name="late.md", file_path="", file_size=1, file_hash="",
        summary="", doc_type="",
    )
    with pytest.raises(KeyError):
        if joined:
            with world.runtime.database.write() as db:
                store.insert_source(**arguments, connection=db)
        else:
            store.insert_source(**arguments)
    assert world.sql.count("SELECT COUNT(*) AS c FROM sources WHERE id='late-source'") == 0


def assert_candidate_admission(world, monkeypatch, status: str) -> None:
    service = world.runtime.memory_service
    real_create = service.store.create_candidate_with_initial_revision

    def create_after_delete(write, actor, reason):
        # The real service preflight has succeeded. A concurrent deletion
        # commits before its content write can acquire the notebook lock.
        world.sql.write("UPDATE notebooks SET status=? WHERE id=?", (status, world.owner_target))
        return real_create(write, actor, reason)

    monkeypatch.setattr(service.store, "create_candidate_with_initial_revision", create_after_delete)
    with pytest.raises(PermissionError):
        service.create_candidate(
            world.owner_target, world.owner.id, None, "late-request", "Title", "Body",
            [], "reason", {}, [],
        )
    assert world.sql.count(
        "SELECT COUNT(*) AS c FROM memory_items WHERE notebook_id=?", (world.owner_target,),
    ) == 0


def assert_root_admission(world, status: str, root: str) -> None:
    world.sql.write("UPDATE notebooks SET status=? WHERE id=?", (status, world.owner_target))
    calls = {
        "report": lambda: world.runtime.report_store.create_report(world.owner_target, "Question"),
        "mount": lambda: world.runtime.notebook_store.replace_mounts(
            world.owner_target, [world.member_target], world.owner.id,
        ),
        "mount_target": lambda: world.runtime.notebook_store.replace_mounts(
            world.member_target, [world.owner_target], world.member.id,
        ),
        "share": lambda: world.runtime.sharing_store.set_share_token(world.owner_target, "late-token"),
        "knowhow": lambda: world.runtime.knowhow_store.create_knowhow_table(
            world.owner_target, "Title", "", [{"name": "Text", "role": "attribute"}],
        ),
        "knowhow_import": lambda: world.runtime.knowhow_store.create_knowhow_table_with_rows(
            world.owner_target, "Title", "", [{"name": "Text", "role": "attribute"}], [["Value"]],
        ),
    }
    with pytest.raises(KeyError):
        calls[root]()
    for table in ("reports", "notebook_bases", "knowhow_tables"):
        assert world.sql.count(
            f"SELECT COUNT(*) AS c FROM {table} WHERE notebook_id=?", (world.owner_target,),
        ) == 0


def assert_existing_disabled_mount_is_preserved(world, status: str) -> None:
    store = world.runtime.notebook_store
    store.replace_mounts(world.member_target, [world.owner_target], world.member.id)
    world.sql.write("UPDATE notebooks SET status=? WHERE id=?", (status, world.owner_target))
    store.replace_mounts(world.member_target, [world.owner_target], world.member.id)
    assert world.sql.count(
        "SELECT COUNT(*) AS c FROM notebook_bases WHERE notebook_id=? AND base_notebook_id=?",
        (world.member_target, world.owner_target),
    ) == 1
