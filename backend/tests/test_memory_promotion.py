from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.models.schemas import AskResponse, Citation, NotebookCreate
from app.services.sqlite_repository import (
    SQLiteRepository,
    reset_request_user,
    set_request_user,
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'memory-promotion.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


@pytest.fixture
def promotion_setup(repo):
    owner = repo.create_user("m00108001", "pw")
    other = repo.create_user("n00108002", "pw")
    token = set_request_user(owner)
    try:
        notebook = repo.create_notebook(NotebookCreate(name="Private memory"))
    finally:
        reset_request_user(token)
    base = repo.create_notebook(NotebookCreate(name="Base corpus"))
    repo.mark_notebook_base(base.id)
    with repo._write() as db:
        db.execute(
            "INSERT INTO agent_profiles "
            "(id,owner_id,name,description,status,created_at,updated_at) "
            "VALUES ('agent-memory',?,'Agent','private client','active','t','t')",
            (owner.id,),
        )
    candidate = repo.create_memory_candidate(
        notebook.id,
        owner.id,
        "agent-memory",
        "promotion-request",
        "RC compensation",
        "RC compensation improves stability.\n\n$f_p = 1/(2\\pi RC)$\n\n"
        "1. Measure R.\n2. Select C.",
        ["compensation"],
        "private proposal reason",
        {"secret_task": "customer alpha", "token": "never-export"},
        [{"source_id": "unverified-private-source", "quoted_span": "do not export"}],
    )
    confirmed = repo.confirm_memory(candidate.id, owner.id)
    return owner, other, notebook, base, confirmed


def test_only_creator_can_propose_confirmed_memory(repo, promotion_setup):
    owner, other, _notebook, _base, memory = promotion_setup

    with pytest.raises((KeyError, PermissionError)):
        repo.propose_memory_promotion(memory.id, other.id)

    proposed = repo.propose_memory_promotion(memory.id, owner.id)
    assert proposed["object_id"] == memory.id
    assert proposed["object_type"] == "memory"
    assert proposed["source_kind"] == "memory"


@pytest.mark.parametrize("terminal_action", ["candidate", "rejected", "deprecated"])
def test_only_confirmed_memory_can_be_proposed(repo, promotion_setup, terminal_action):
    owner, _other, notebook, _base, confirmed = promotion_setup
    if terminal_action == "candidate":
        memory = repo.create_memory_candidate(
            notebook.id, owner.id, "agent-memory", "still-candidate",
            "Candidate", "Not confirmed", [], "reason", {}, [],
        )
    elif terminal_action == "rejected":
        memory = repo.create_memory_candidate(
            notebook.id, owner.id, "agent-memory", "rejected-memory",
            "Rejected", "Rejected", [], "reason", {}, [],
        )
        memory = repo.reject_memory(memory.id, owner.id)
    else:
        memory = repo.deprecate_memory(confirmed.id, owner.id)

    with pytest.raises(ValueError):
        repo.propose_memory_promotion(memory.id, owner.id)


def test_proposal_reuses_queue_without_exposing_private_provenance(repo, promotion_setup):
    owner, _other, notebook, _base, memory = promotion_setup

    proposal = repo.propose_memory_promotion(memory.id, owner.id)
    repeated = repo.propose_memory_promotion(memory.id, owner.id)
    assert repeated["id"] == proposal["id"]
    assert {item["object_type"] for item in proposal["payload"]["candidates"]} == {
        "concept", "claim", "formula", "procedure",
    }
    rendered = json.dumps(proposal, ensure_ascii=False)
    assert "customer alpha" not in rendered
    assert "never-export" not in rendered
    assert "private proposal reason" not in rendered
    assert "unverified-private-source" not in rendered
    assert repo.get_memory(memory.id, owner.id).status == "confirmed"
    assert repo.get_memory(memory.id, owner.id).promotion_state == "proposed"
    queue = repo.list_promotion_queue()
    queued = next(item for item in queue if item["id"] == proposal["id"])
    assert queued["source_kind"] == "memory"
    assert queued["notebook_id"] == notebook.id


def test_admin_approval_is_idempotent_and_keeps_memory_private(repo, promotion_setup):
    owner, other, notebook, base, memory = promotion_setup
    proposal = repo.propose_memory_promotion(memory.id, owner.id)

    first = repo.approve_promotion(proposal["id"])
    second = repo.approve_promotion(proposal["id"])
    assert first["base_object_ids"] == second["base_object_ids"]
    assert len(first["base_object_ids"]) == 4
    with repo._connect() as db:
        base_rows = db.execute(
            "SELECT id,object_type,payload,evidence,status FROM knowledge_objects "
            "WHERE notebook_id=? ORDER BY object_type,id", (base.id,),
        ).fetchall()
    assert {row["id"] for row in base_rows} == set(first["base_object_ids"])
    assert {row["object_type"] for row in base_rows} == {
        "concept", "claim", "formula", "procedure",
    }
    assert all(row["status"] == "approved" for row in base_rows)
    assert "customer alpha" not in json.dumps(
        [dict(row) for row in base_rows], ensure_ascii=False
    )
    current = repo.get_memory(memory.id, owner.id)
    assert current.status == "confirmed"
    assert current.promotion_state == "approved"
    assert current.notebook_id == notebook.id
    assert current.created_by == owner.id
    with pytest.raises(KeyError):
        repo.get_memory(memory.id, other.id)
    revisions = repo.memory_revisions(memory.id, owner.id)
    assert revisions[-1].promotion_state == "approved"
    assert set(current.provenance["kg_promotion"]["base_object_ids"]) == set(
        first["base_object_ids"]
    )


def test_admin_cannot_approve_after_owner_deprecates_proposed_memory(
    repo, promotion_setup
):
    owner, _other, _notebook, base, memory = promotion_setup
    proposal = repo.propose_memory_promotion(memory.id, owner.id)
    repo.deprecate_memory(memory.id, owner.id)

    with pytest.raises(ValueError):
        repo.approve_promotion(proposal["id"])
    with repo._connect() as db:
        count = db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=?",
            (base.id,),
        ).fetchone()["c"]
    assert count == 0


def test_memory_promotion_dedupes_into_existing_base_object(repo, promotion_setup):
    owner, _other, _notebook, base, memory = promotion_setup
    existing = repo._test_insert_object(
        base.id, "concept", {"name": "compensation"}
    )
    proposal = repo.propose_memory_promotion(memory.id, owner.id)
    result = repo.approve_promotion(proposal["id"])
    assert existing in result["base_object_ids"]
    with repo._connect() as db:
        count = db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_objects "
            "WHERE notebook_id=? AND object_type='concept'", (base.id,),
        ).fetchone()["c"]
    assert count == 1


def test_approval_binds_only_server_validated_source_element_evidence(
    repo, promotion_setup
):
    owner, _other, notebook, base, _memory = promotion_setup
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,parse_status,file_name,file_path,"
            "file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES ('source-safe',?,'Approved paper','markdown','extracted','parsed',"
            "'paper.md','',0,'','','note','t','t')",
            (notebook.id,),
        )
        db.execute(
            "INSERT INTO source_elements "
            "(id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES ('element-safe','source-safe','paragraph','p1',"
            "'Verified source statement.','{}','t')"
        )
    answer_id = repo._runtime.ask_state.save_answer(
        notebook.id,
        None,
        "What is verified?",
        AskResponse(
            conclusion="Verified source statement.",
            answer="Verified source statement.",
            citations=[
                Citation(
                    label="k1",
                    source_id="source-safe",
                    element_id="element-safe",
                    location_label="p1",
                    quoted_span="forged client quote",
                )
            ],
        ),
        owner.id,
    )
    memory = repo.create_memory_from_answer(
        notebook.id,
        owner.id,
        answer_id,
        "Verified claim",
        "Verified source statement.",
        [],
    )
    proposal = repo.propose_memory_promotion(memory.id, owner.id)
    queued = next(
        item for item in repo.list_promotion_queue() if item["id"] == proposal["id"]
    )
    assert queued["evidence"][0].quoted_span == "Verified source statement."
    assert "forged client quote" not in queued["evidence"][0].quoted_span
    result = repo.approve_promotion(proposal["id"])
    with repo._connect() as db:
        row = db.execute(
            "SELECT evidence FROM knowledge_objects WHERE notebook_id=? AND id=?",
            (base.id, result["base_object_ids"][0]),
        ).fetchone()
    evidence = json.loads(row["evidence"])
    assert evidence == [
        {
            "source_id": "source-safe",
            "source_title": "Approved paper",
            "element_id": "element-safe",
            "element_type": "paragraph",
            "location_label": "p1",
            "quoted_span": "Verified source statement.",
            "confidence": 1.0,
        }
    ]
    assert "forged client quote" not in json.dumps(evidence)


def test_admin_rejection_records_state_but_leaves_confirmed_memory(repo, promotion_setup):
    owner, _other, notebook, _base, memory = promotion_setup
    proposal = repo.propose_memory_promotion(memory.id, owner.id)
    rejected = repo.reject_promotion(proposal["id"], "not canonical")
    assert rejected["status"] == "rejected"
    current = repo.get_memory(memory.id, owner.id)
    assert current.status == "confirmed"
    assert current.promotion_state == "rejected"
    assert current.notebook_id == notebook.id
    assert current.provenance["kg_promotion"]["reason"] == "not canonical"


def test_memory_promotion_api_is_owner_only_and_admin_queue_reuses_existing_routes(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import create_app
    from app.api.deps import repository

    client = TestClient(create_app())
    registered = client.post(
        "/api/auth/register", json={"username": "p00108003", "password": "pw"}
    ).json()
    headers = {"Authorization": f"Bearer {registered['token']}"}
    user_id = registered["user"]["id"]
    notebook_id = client.post(
        "/api/notebooks", headers=headers, json={"name": "Memory API promotion"}
    ).json()["id"]
    repo_api = repository()
    candidate = repo_api.create_memory_candidate(
        notebook_id, user_id, None, "api-promotion", "Claim", "A grounded claim",
        [], "reason", {}, [],
    )
    memory = repo_api.confirm_memory(candidate.id, user_id)

    response = client.post(f"/api/memories/{memory.id}/promote", headers=headers)
    assert response.status_code == 201, response.text
    assert response.json()["source_kind"] == "memory"
    assert client.get("/api/promotion-queue", headers=headers).status_code == 403
