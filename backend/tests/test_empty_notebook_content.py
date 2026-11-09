"""Standalone authored content and Agent bindings retain deletion recovery."""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from tests.notebook_delete_grace_cases import DEADLINE, T0, build_world, iso


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'empty-content.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    repo = SQLiteRepository(Settings(_env_file=None))
    try:
        yield build_world(repo, postgres=False, monkeypatch=monkeypatch)
    finally:
        repo.close()


@pytest.mark.parametrize("content", ["understanding", "observation", "default_token", "experience"])
def test_standalone_content_retains_the_window_and_can_be_restored(world, content):
    notebook_id = world.owner_target
    now = iso(T0)
    if content == "understanding":
        world.sql.write(
            "INSERT INTO agent_notebook_profile "
            "(notebook_id,owner_id,label,value,updated_origin,created_at,updated_at) "
            "VALUES (?,?,'expertise','手写的领域知识','user',?,?)",
            (notebook_id, world.owner.id, now, now),
        )
        table, column = "agent_notebook_profile", "notebook_id"
    elif content == "observation":
        world.sql.write(
            "INSERT INTO agent_observations (id,notebook_id,owner_id,text,created_at) "
            "VALUES ('obs-only',?,?,'尚未整理为记忆的记录',?)",
            (notebook_id, world.owner.id, now),
        )
        table, column = "agent_observations", "notebook_id"
    elif content == "default_token":
        world.sql.write(
            "INSERT INTO agent_profiles (id,owner_id,name,created_at,updated_at) "
            "VALUES ('profile-only',?,'Agent',?,?)",
            (world.owner.id, now, now),
        )
        world.sql.write(
            "INSERT INTO agent_access_tokens "
            "(id,agent_profile_id,token_hash,default_notebook_id,created_at) "
            "VALUES ('token-only','profile-only','test-hash',?,?)",
            (notebook_id, now),
        )
        table, column = "agent_access_tokens", "default_notebook_id"
    else:
        world.sql.write(
            "INSERT INTO retrieval_experiences (id,notebook_id,action,created_at,updated_at) "
            "VALUES ('experience-only',?,'先查工艺约束',?,?)",
            (notebook_id, now, now),
        )
        table, column = "retrieval_experiences", "notebook_id"
    before = world.sql.rows(f"SELECT * FROM {table} WHERE {column}=?", (notebook_id,))
    assert before
    stores = world.runtime.notebook_delete_jobs
    result = stores.request_pending(
        notebook_id, world.owner.id, now=T0, delete_empty_immediately=True,
    )
    assert result == {"notebook_id": notebook_id, "delete_after": iso(DEADLINE)}
    assert world.sql.rows("SELECT id FROM notebook_delete_jobs WHERE notebook_id=?", (notebook_id,)) == []
    assert stores.cancel_pending(notebook_id, now=T0) == "cancelled"
    assert world.sql.rows(f"SELECT * FROM {table} WHERE {column}=?", (notebook_id,)) == before


@pytest.mark.parametrize("status", ["pending_delete", "deleting"])
def test_manual_understanding_cannot_be_created_after_delete(world, status):
    notebook_id = world.owner_target
    world.sql.write("UPDATE notebooks SET status=? WHERE id=?", (status, notebook_id))
    with pytest.raises(KeyError):
        world.runtime.agent_profile.write_block(
            notebook_id, world.owner.id, "expertise", value="迟到的编辑", evidence=[],
            expected_revision=0, origin="user", actor=world.owner.id,
        )
    assert world.sql.rows(
        "SELECT * FROM agent_notebook_profile WHERE notebook_id=?", (notebook_id,),
    ) == []
