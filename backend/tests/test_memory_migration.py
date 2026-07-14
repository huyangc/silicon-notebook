from __future__ import annotations

import sqlite3

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.models import schemas
from app.services import sqlite_repository as sr


@pytest.fixture
def repo(tmp_path):
    return sr.SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path}/memory.db",
            storage_dir=str(tmp_path / "storage"),
        )
    )


def _names(db: sqlite3.Connection, kind: str) -> set[str]:
    return {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type=?", (kind,)
        )
    }


def test_v13_memory_schema_has_privacy_and_agent_indexes(repo):
    # >= 而非 ==:后续迁移(如 Task 1 的 _migration_14)会继续推高 SCHEMA_VERSION,
    # 这里只断言 Memory/Agent 那一步(_migration_13)已落地,不锁死为全局当前版本号。
    assert sr.SCHEMA_VERSION >= 13
    with repo._connect() as db:
        tables = _names(db, "table")
        assert {
            "memory_items",
            "memory_revisions",
            "memory_provenance",
            "memory_embeddings",
            "agent_profiles",
            "agent_access_tokens",
            "agent_token_notebooks",
        } <= tables
        indexes = _names(db, "index")
        assert "idx_memory_answer_once" in indexes
        assert "idx_memory_owner_notebook_status" in indexes
        assert "idx_memory_agent_candidate" in indexes
        token_columns = {
            row["name"]: row for row in db.execute("PRAGMA table_info(agent_access_tokens)")
        }
        assert token_columns["default_notebook_id"]["notnull"] == 1
        default_notebook_fk = next(
            row
            for row in db.execute("PRAGMA foreign_key_list(agent_access_tokens)")
            if row["from"] == "default_notebook_id"
        )
        assert default_notebook_fk["on_delete"] == "CASCADE"


def test_master_v12_database_upgrades_to_v13_memory_schema(repo):
    """Master reached v12 without Memory before the branches were merged."""
    with repo._write() as db:
        db.execute("DROP TABLE memory_items_fts")
        for table in (
            "memory_embeddings",
            "memory_provenance",
            "memory_revisions",
            "memory_items",
            "agent_token_notebooks",
            "agent_access_tokens",
            "agent_profiles",
        ):
            db.execute(f"DROP TABLE {table}")
        db.execute("PRAGMA user_version = 12")
    repo.close_local()

    upgraded = sr.SQLiteRepository(repo.settings)
    with upgraded._connect() as db:
        # 非硬编码字面量:落到当前 SCHEMA_VERSION,防后续新迁移使断言假红。
        assert db.execute("PRAGMA user_version").fetchone()[0] == sr.SCHEMA_VERSION
        assert {
            "memory_items",
            "memory_revisions",
            "memory_provenance",
            "memory_embeddings",
            "agent_profiles",
            "agent_access_tokens",
            "agent_token_notebooks",
        } <= _names(db, "table")


def test_feature_v11_database_upgrades_to_v13_master_indexes(repo):
    """The pre-merge Memory branch used v11 without master's later indexes."""
    master_indexes = {
        "idx_element_embeddings_source",
        "idx_knowledge_relations_nb_review",
        "idx_comentions_nb_b",
        "idx_sources_nb_parse_status",
    }
    with repo._write() as db:
        for index in master_indexes:
            db.execute(f"DROP INDEX {index}")
        db.execute("PRAGMA user_version = 11")
    repo.close_local()

    upgraded = sr.SQLiteRepository(repo.settings)
    with upgraded._connect() as db:
        # 非硬编码字面量:落到当前 SCHEMA_VERSION,防后续新迁移使断言假红。
        assert db.execute("PRAGMA user_version").fetchone()[0] == sr.SCHEMA_VERSION
        assert master_indexes <= _names(db, "index")


def test_v13_memory_schema_enforces_lifecycle_and_answer_idempotency(repo):
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebooks (id,name,created_by,created_at,updated_at) "
            "VALUES ('nb-memory','Memory','user-local','t','t')"
        )
        values = (
            "memory-1",
            "nb-memory",
            "user-local",
            "answer-1",
            "ask_answer",
            "confirmed",
            "Title",
            "Body",
            "t",
            "t",
        )
        db.execute(
            "INSERT INTO memory_items "
            "(id,notebook_id,created_by,source_answer_id,origin,status,title,content_md,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            values,
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO memory_items "
                "(id,notebook_id,created_by,source_answer_id,origin,status,title,content_md,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("memory-2", *values[1:]),
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO memory_items "
                "(id,notebook_id,created_by,origin,status,title,content_md,created_at,updated_at) "
                "VALUES ('memory-bad','nb-memory','user-local','automatic','confirmed','T','B','t','t')"
            )


def test_v13_memory_fts_tracks_insert_update_and_delete(repo):
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebooks (id,name,created_by,created_at,updated_at) "
            "VALUES ('nb-fts','Memory','user-local','t','t')"
        )
        db.execute(
            "INSERT INTO memory_items "
            "(id,notebook_id,created_by,origin,status,title,content_md,tags_json,created_at,updated_at) "
            "VALUES ('memory-fts','nb-fts','user-local','external_agent','candidate',"
            "'Loop stability','phase margin','[\"analog\"]','t','t')"
        )
        assert db.execute(
            "SELECT rowid FROM memory_items_fts WHERE memory_items_fts MATCH 'stability'"
        ).fetchone()
        db.execute(
            "UPDATE memory_items SET title='Noise analysis' WHERE id='memory-fts'"
        )
        assert not db.execute(
            "SELECT rowid FROM memory_items_fts WHERE memory_items_fts MATCH 'stability'"
        ).fetchone()
        assert db.execute(
            "SELECT rowid FROM memory_items_fts WHERE memory_items_fts MATCH 'noise'"
        ).fetchone()
        db.execute("DELETE FROM memory_items WHERE id='memory-fts'")
        assert not db.execute(
            "SELECT rowid FROM memory_items_fts WHERE memory_items_fts MATCH 'noise'"
        ).fetchone()


def test_v13_memory_fts_finds_unsegmented_chinese_substring(repo):
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebooks (id,name,created_by,created_at,updated_at) "
            "VALUES ('nb-fts-zh','Memory','user-local','t','t')"
        )
        db.execute(
            "INSERT INTO memory_items "
            "(id,notebook_id,created_by,origin,status,title,content_md,created_at,updated_at) "
            "VALUES ('memory-fts-zh','nb-fts-zh','user-local','external_agent','candidate',"
            "'模拟电路稳定性分析','相位裕度与环路增益','t','t')"
        )
        assert db.execute(
            "SELECT rowid FROM memory_items_fts WHERE memory_items_fts MATCH '稳定性'"
        ).fetchone()


def test_memory_models_expose_safe_defaults_and_lifecycle_values():
    expected_models = {
        "MemoryRecord",
        "PaginatedMemories",
        "MemoryPreview",
        "MemoryCreateFromAnswer",
        "MemoryUpdate",
        "MemoryReviewRequest",
    }
    assert expected_models <= set(dir(schemas))
    MemoryPreview = schemas.MemoryPreview
    MemoryCreateFromAnswer = schemas.MemoryCreateFromAnswer
    MemoryUpdate = schemas.MemoryUpdate
    MemoryReviewRequest = schemas.MemoryReviewRequest
    MemoryRecord = schemas.MemoryRecord
    PaginatedMemories = schemas.PaginatedMemories
    preview = MemoryPreview(title="T", content_md="B")
    assert preview.tags == []
    assert preview.provenance_summary == {}

    create = MemoryCreateFromAnswer(answer_id="answer-1", title="T", content_md="B")
    assert create.tags == []
    assert MemoryUpdate().model_dump(exclude_none=True) == {}
    assert MemoryReviewRequest().model_dump(exclude_none=True) == {}

    record = MemoryRecord(
        id="memory-1",
        notebook_id="nb-1",
        created_by="user-1",
        origin="ask_answer",
        status="confirmed",
        title="T",
        content_md="B",
        created_at="t",
        updated_at="t",
    )
    assert record.tags == []
    assert record.promotion_state == "none"
    assert record.embedding_status == "pending"
    page = PaginatedMemories(items=[record], total_count=1, offset=0, limit=50)
    assert page.items[0].id == "memory-1"

    with pytest.raises(ValidationError):
        MemoryRecord(
            id="bad",
            notebook_id="nb-1",
            created_by="user-1",
            origin="automatic",
            status="confirmed",
            title="T",
            content_md="B",
            created_at="t",
            updated_at="t",
        )


def test_agent_models_keep_plaintext_token_on_issued_response_only():
    expected_models = {"AgentProfile", "AgentTokenCreate", "AgentTokenIssued"}
    assert expected_models <= set(dir(schemas))
    AgentProfile = schemas.AgentProfile
    AgentTokenCreate = schemas.AgentTokenCreate
    AgentTokenIssued = schemas.AgentTokenIssued
    profile = AgentProfile(
        id="agent-1",
        owner_id="user-1",
        name="Codex",
        created_at="t",
        updated_at="t",
    )
    assert profile.status == "active"
    assert profile.description == ""

    request = AgentTokenCreate(
        agent_profile_id="agent-1",
        scopes=["context:read", "memory:propose"],
        default_notebook_id="nb-1",
        notebook_ids=["nb-1"],
    )
    assert "token" not in AgentTokenCreate.model_fields
    issued = AgentTokenIssued(
        id="token-1",
        token="sn_agent_secret",
        agent_profile_id="agent-1",
        scopes=request.scopes,
        default_notebook_id=request.default_notebook_id,
        notebook_ids=request.notebook_ids,
        created_at="t",
    )
    assert issued.token == "sn_agent_secret"
    assert issued.default_notebook_id == "nb-1"

    with pytest.raises(ValidationError):
        AgentTokenCreate(agent_profile_id="agent-1")
    with pytest.raises(ValidationError):
        AgentTokenCreate(agent_profile_id="agent-1", default_notebook_id="")
    with pytest.raises(ValidationError):
        AgentTokenIssued(
            id="token-2",
            token="sn_agent_secret",
            agent_profile_id="agent-1",
            default_notebook_id="",
            created_at="t",
        )
