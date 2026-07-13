from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

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


def test_memory_promotion_pins_revision_snapshot_for_queue(repo, promotion_setup):
    owner, _other, _notebook, _base, memory = promotion_setup

    proposal = repo.propose_memory_promotion(memory.id, owner.id)
    queue_item = next(
        item for item in repo.list_promotion_queue() if item["id"] == proposal["id"]
    )

    assert proposal["source_revision"] > 0
    assert queue_item["source_revision"] == proposal["source_revision"]
    assert queue_item["payload"] == proposal["payload"]
    assert queue_item["payload"]["candidates"]


def test_editing_proposed_memory_supersedes_snapshot_and_allows_reproposal(
    repo, promotion_setup
):
    owner, _other, _notebook, _base, memory = promotion_setup
    original = repo.propose_memory_promotion(memory.id, owner.id)

    edited = repo.update_memory(
        memory.id,
        owner.id,
        {"title": "Updated RC compensation", "content_md": "Updated claim."},
    )

    assert edited.status == "confirmed"
    assert edited.promotion_state == "none"
    with repo._connect() as db:
        old_row = db.execute(
            "SELECT status,reason,reviewed_by FROM promotion_candidates WHERE id=?",
            (original["id"],),
        ).fetchone()
    assert dict(old_row) == {
        "status": "rejected",
        "reason": "superseded_by_memory_edit",
        "reviewed_by": owner.id,
    }

    replacement = repo.propose_memory_promotion(memory.id, owner.id)
    assert replacement["id"] != original["id"]
    assert replacement["source_revision"] > original["source_revision"]
    assert replacement["payload"]["candidates"] != original["payload"]["candidates"]

    old_queue = next(
        item
        for item in repo.list_promotion_queue(status_filter="rejected")
        if item["id"] == original["id"]
    )
    assert old_queue["source_revision"] == original["source_revision"]
    assert old_queue["payload"] == original["payload"]


def test_approval_fails_without_side_effect_when_pinned_revision_mismatches(
    repo, promotion_setup
):
    owner, _other, _notebook, base, memory = promotion_setup
    proposal = repo.propose_memory_promotion(memory.id, owner.id)
    with repo._write() as db:
        db.execute(
            "UPDATE memory_items SET content_md='tampered without invalidation' WHERE id=?",
            (memory.id,),
        )
    with repo._connect() as db:
        before_candidate = dict(
            db.execute(
                "SELECT status,reviewed_by,base_match_id FROM promotion_candidates WHERE id=?",
                (proposal["id"],),
            ).fetchone()
        )
        before_revisions = db.execute(
            "SELECT COUNT(*) AS c FROM memory_revisions WHERE memory_id=?",
            (memory.id,),
        ).fetchone()["c"]

    with pytest.raises(ValueError, match="revision|snapshot"):
        repo.approve_promotion_as_reviewer(proposal["id"], "admin-reviewer")

    with repo._connect() as db:
        after_candidate = dict(
            db.execute(
                "SELECT status,reviewed_by,base_match_id FROM promotion_candidates WHERE id=?",
                (proposal["id"],),
            ).fetchone()
        )
        after_revisions = db.execute(
            "SELECT COUNT(*) AS c FROM memory_revisions WHERE memory_id=?",
            (memory.id,),
        ).fetchone()["c"]
        base_count = db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=?",
            (base.id,),
        ).fetchone()["c"]
    assert after_candidate == before_candidate
    assert after_revisions == before_revisions
    assert base_count == 0


def test_edit_and_approval_race_has_only_a_fully_approved_or_superseded_outcome(
    repo, promotion_setup
):
    owner, _other, _notebook, base, memory = promotion_setup
    proposal = repo.propose_memory_promotion(memory.id, owner.id)
    barrier = threading.Barrier(2)

    def approve():
        barrier.wait()
        try:
            return ("approved", repo.approve_promotion(proposal["id"]))
        except ValueError as exc:
            return ("approval_rejected", str(exc))

    def edit():
        barrier.wait()
        return (
            "edited",
            repo.update_memory(
                memory.id,
                owner.id,
                {"content_md": "Concurrent edit must not change a pinned review."},
            ),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [pool.submit(approve), pool.submit(edit)]
        results = [future.result(timeout=10) for future in outcomes]

    with repo._connect() as db:
        candidate = db.execute(
            "SELECT status,reason FROM promotion_candidates WHERE id=?",
            (proposal["id"],),
        ).fetchone()
        base_count = db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=?",
            (base.id,),
        ).fetchone()["c"]
    current = repo.get_memory(memory.id, owner.id)
    if candidate["status"] == "approved":
        assert base_count > 0
        assert current.promotion_state == "approved"
        assert results[0][0] == "approved"
    else:
        assert dict(candidate) == {
            "status": "rejected",
            "reason": "superseded_by_memory_edit",
        }
        assert base_count == 0
        assert current.promotion_state == "none"
        assert results[0][0] == "approval_rejected"


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
    before = repo.memory_revisions(memory.id, owner.id)
    deprecated = repo.deprecate_memory(memory.id, owner.id)

    assert deprecated.status == "deprecated"
    assert deprecated.promotion_state == "none"
    assert deprecated.provenance["kg_promotion"]["state"] == "superseded"
    assert deprecated.provenance["kg_promotion"]["proposal_id"] == proposal["id"]
    assert deprecated.provenance["kg_promotion_snapshots"][proposal["id"]][
        "source_revision"
    ] == proposal["source_revision"]
    with repo._connect() as db:
        candidate = dict(db.execute(
            "SELECT status,reason,reviewed_by FROM promotion_candidates WHERE id=?",
            (proposal["id"],),
        ).fetchone())
    assert candidate == {
        "status": "rejected",
        "reason": "superseded_by_memory_terminal_status",
        "reviewed_by": owner.id,
    }
    assert len(repo.memory_revisions(memory.id, owner.id)) == len(before) + 1
    assert not any(
        item["id"] == proposal["id"] for item in repo.list_promotion_queue()
    )

    with pytest.raises(ValueError):
        repo.approve_promotion(proposal["id"])
    with repo._connect() as db:
        count = db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=?",
            (base.id,),
        ).fetchone()["c"]
    assert count == 0


def test_owner_cannot_reject_proposed_confirmed_memory_or_mutate_its_queue(
    repo, promotion_setup
):
    owner, _other, _notebook, base, memory = promotion_setup
    proposal = repo.propose_memory_promotion(memory.id, owner.id)

    with pytest.raises(ValueError, match="confirmed -> rejected"):
        repo.reject_memory(memory.id, owner.id)

    current = repo.get_memory(memory.id, owner.id)
    assert current.status == "confirmed"
    assert current.promotion_state == "proposed"
    assert current.provenance["kg_promotion"]["state"] == "proposed"
    with repo._connect() as db:
        candidate = dict(db.execute(
            "SELECT status,reason,reviewed_by FROM promotion_candidates WHERE id=?",
            (proposal["id"],),
        ).fetchone())
        base_count = db.execute(
            "SELECT COUNT(*) AS count FROM knowledge_objects WHERE notebook_id=?",
            (base.id,),
        ).fetchone()["count"]
    assert candidate == {
        "status": "proposed",
        "reason": "",
        "reviewed_by": "",
    }
    assert base_count == 0
    assert any(
        item["id"] == proposal["id"] for item in repo.list_promotion_queue()
    )


def test_deprecate_transition_and_approval_race_is_atomic(repo, promotion_setup):
    owner, _other, _notebook, base, memory = promotion_setup
    proposal = repo.propose_memory_promotion(memory.id, owner.id)
    barrier = threading.Barrier(2)

    def approve():
        barrier.wait()
        try:
            return ("approved", repo.approve_promotion(proposal["id"]))
        except ValueError as exc:
            return ("approval_rejected", str(exc))

    def terminate():
        barrier.wait()
        return ("terminal", repo.deprecate_memory(memory.id, owner.id))

    with ThreadPoolExecutor(max_workers=2) as pool:
        approval_future = pool.submit(approve)
        terminal_future = pool.submit(terminate)
        approval_result = approval_future.result(timeout=10)
        terminal_result = terminal_future.result(timeout=10)

    current = repo.get_memory(memory.id, owner.id)
    with repo._connect() as db:
        candidate = dict(db.execute(
            "SELECT status,reason FROM promotion_candidates WHERE id=?",
            (proposal["id"],),
        ).fetchone())
        base_count = db.execute(
            "SELECT COUNT(*) AS count FROM knowledge_objects WHERE notebook_id=?",
            (base.id,),
        ).fetchone()["count"]
    assert terminal_result[0] == "terminal"
    assert current.status == "deprecated"
    if candidate["status"] == "rejected":
        assert candidate["reason"] == "superseded_by_memory_terminal_status"
        assert current.promotion_state == "none"
        assert base_count == 0
        assert approval_result[0] == "approval_rejected"
    else:
        assert candidate["status"] == "approved"
        assert current.promotion_state == "approved"
        assert base_count > 0
        assert approval_result[0] == "approved"


def test_admin_approval_fails_closed_after_member_loses_notebook_access(repo):
    notebook_owner = repo.create_user("s00108004", "pw")
    memory_owner = repo.create_user("t00108005", "pw")
    token = set_request_user(notebook_owner)
    try:
        notebook = repo.create_notebook(NotebookCreate(name="Shared promotion"))
    finally:
        reset_request_user(token)
    base = repo.create_notebook(NotebookCreate(name="Shared promotion base"))
    repo.mark_notebook_base(base.id)
    repo.add_member(notebook.id, memory_owner.id)
    memory = repo.create_memory_candidate(
        notebook.id,
        memory_owner.id,
        None,
        "member-promotion",
        "Member claim",
        "A member-owned confirmed claim.",
        [],
        "reason",
        {},
        [],
    )
    memory = repo.confirm_memory(memory.id, memory_owner.id)
    proposal = repo.propose_memory_promotion(memory.id, memory_owner.id)
    with repo._connect() as db:
        before_revision_count = db.execute(
            "SELECT COUNT(*) AS c FROM memory_revisions WHERE memory_id=?",
            (memory.id,),
        ).fetchone()["c"]
        before_provenance = db.execute(
            "SELECT payload_json FROM memory_provenance WHERE memory_id=?",
            (memory.id,),
        ).fetchone()["payload_json"]

    repo.remove_member(notebook.id, memory_owner.id)
    with pytest.raises(PermissionError):
        repo.approve_promotion(proposal["id"])

    with repo._connect() as db:
        promotion = db.execute(
            "SELECT status FROM promotion_candidates WHERE id=?", (proposal["id"],)
        ).fetchone()
        stored_memory = db.execute(
            "SELECT status,promotion_state,notebook_id,created_by "
            "FROM memory_items WHERE id=?", (memory.id,),
        ).fetchone()
        revision_count = db.execute(
            "SELECT COUNT(*) AS c FROM memory_revisions WHERE memory_id=?",
            (memory.id,),
        ).fetchone()["c"]
        provenance = db.execute(
            "SELECT payload_json FROM memory_provenance WHERE memory_id=?",
            (memory.id,),
        ).fetchone()["payload_json"]
        base_count = db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=?",
            (base.id,),
        ).fetchone()["c"]
    assert promotion["status"] == "proposed"
    assert dict(stored_memory) == {
        "status": "confirmed",
        "promotion_state": "proposed",
        "notebook_id": notebook.id,
        "created_by": memory_owner.id,
    }
    assert revision_count == before_revision_count
    assert provenance == before_provenance
    assert base_count == 0


def test_admin_approval_fails_closed_on_candidate_memory_notebook_mismatch(
    repo, promotion_setup
):
    owner, _other, _notebook, base, memory = promotion_setup
    token = set_request_user(owner)
    try:
        other_notebook = repo.create_notebook(NotebookCreate(name="Wrong notebook"))
    finally:
        reset_request_user(token)
    proposal = repo.propose_memory_promotion(memory.id, owner.id)
    with repo._write() as db:
        db.execute(
            "UPDATE promotion_candidates SET notebook_id=? WHERE id=?",
            (other_notebook.id, proposal["id"]),
        )
    with repo._connect() as db:
        before_revision_count = db.execute(
            "SELECT COUNT(*) AS c FROM memory_revisions WHERE memory_id=?",
            (memory.id,),
        ).fetchone()["c"]
        before_provenance = db.execute(
            "SELECT payload_json FROM memory_provenance WHERE memory_id=?",
            (memory.id,),
        ).fetchone()["payload_json"]

    with pytest.raises(ValueError, match="notebook"):
        repo.approve_promotion(proposal["id"])

    with repo._connect() as db:
        promotion_status = db.execute(
            "SELECT status FROM promotion_candidates WHERE id=?", (proposal["id"],)
        ).fetchone()["status"]
        memory_state = db.execute(
            "SELECT promotion_state FROM memory_items WHERE id=?", (memory.id,)
        ).fetchone()["promotion_state"]
        revision_count = db.execute(
            "SELECT COUNT(*) AS c FROM memory_revisions WHERE memory_id=?",
            (memory.id,),
        ).fetchone()["c"]
        provenance = db.execute(
            "SELECT payload_json FROM memory_provenance WHERE memory_id=?",
            (memory.id,),
        ).fetchone()["payload_json"]
        base_count = db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=?",
            (base.id,),
        ).fetchone()["c"]
    assert promotion_status == "proposed"
    assert memory_state == "proposed"
    assert revision_count == before_revision_count
    assert provenance == before_provenance
    assert base_count == 0


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


def test_promotion_routes_record_the_authenticated_admin_reviewer(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'reviewers.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import create_app
    from app.api.deps import repository

    client = TestClient(create_app())
    owner_session = client.post(
        "/api/auth/register", json={"username": "u00108101", "password": "pw"}
    ).json()
    reviewer_a = client.post(
        "/api/auth/register", json={"username": "v00108102", "password": "pw"}
    ).json()
    reviewer_b = client.post(
        "/api/auth/register", json={"username": "w00108103", "password": "pw"}
    ).json()
    owner_headers = {"Authorization": f"Bearer {owner_session['token']}"}
    reviewer_a_headers = {"Authorization": f"Bearer {reviewer_a['token']}"}
    reviewer_b_headers = {"Authorization": f"Bearer {reviewer_b['token']}"}
    notebook_id = client.post(
        "/api/notebooks", headers=owner_headers, json={"name": "Reviewer attribution"}
    ).json()["id"]
    repo_api = repository()
    base = repo_api.create_notebook(NotebookCreate(name="Reviewer base"))
    repo_api.mark_notebook_base(base.id)
    with repo_api._write() as db:
        db.execute(
            "UPDATE users SET role='admin' WHERE id IN (?,?)",
            (reviewer_a["user"]["id"], reviewer_b["user"]["id"]),
        )

    approve_memory = repo_api.create_memory_candidate(
        notebook_id, owner_session["user"]["id"], None, "approve-reviewer",
        "Approve", "Approved reviewer claim", [], "", {}, [],
    )
    approve_memory = repo_api.confirm_memory(
        approve_memory.id, owner_session["user"]["id"]
    )
    approve_proposal = repo_api.propose_memory_promotion(
        approve_memory.id, owner_session["user"]["id"]
    )
    approve_response = client.post(
        f"/api/promotion-queue/{approve_proposal['id']}/approve",
        headers=reviewer_a_headers,
    )
    assert approve_response.status_code == 200, approve_response.text

    reject_memory = repo_api.create_memory_candidate(
        notebook_id, owner_session["user"]["id"], None, "reject-reviewer",
        "Reject", "Rejected reviewer claim", [], "", {}, [],
    )
    reject_memory = repo_api.confirm_memory(
        reject_memory.id, owner_session["user"]["id"]
    )
    reject_proposal = repo_api.propose_memory_promotion(
        reject_memory.id, owner_session["user"]["id"]
    )
    reject_response = client.post(
        f"/api/promotion-queue/{reject_proposal['id']}/reject",
        headers=reviewer_b_headers,
        json={"reason": "not canonical"},
    )
    assert reject_response.status_code == 200, reject_response.text

    knowledge_id = repo_api._test_insert_object(
        notebook_id, "concept", {"name": "Authenticated reviewer"}
    )
    knowledge_proposal = repo_api.propose_promotion(notebook_id, knowledge_id)
    knowledge_response = client.post(
        f"/api/promotion-queue/{knowledge_proposal['id']}/approve",
        headers=reviewer_b_headers,
    )
    assert knowledge_response.status_code == 200, knowledge_response.text

    with repo_api._connect() as db:
        reviewed = {
            row["id"]: row["reviewed_by"]
            for row in db.execute(
                "SELECT id,reviewed_by FROM promotion_candidates WHERE id IN (?,?,?)",
                (
                    approve_proposal["id"],
                    reject_proposal["id"],
                    knowledge_proposal["id"],
                ),
            ).fetchall()
        }
    assert reviewed == {
        approve_proposal["id"]: reviewer_a["user"]["id"],
        reject_proposal["id"]: reviewer_b["user"]["id"],
        knowledge_proposal["id"]: reviewer_b["user"]["id"],
    }
