"""
Tests for apply_conflict_resolution (T4 — write-back mechanics).

Uses a real temp-DB SQLiteRepository with FakeEmbedder so payload-modify
re-embed doesn't hit a real endpoint.

Seeds: one notebook + two Claim objects + two knowledge_relations.
"""
import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_embedding_client


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    bind_embedding_client(r, FakeEmbedder(dim=16))
    return r


@pytest.fixture
def seeded(repo):
    """Insert notebook + 2 claim objects + 2 relations. Returns (repo, nb_id, oid_a, oid_b, rel_a, rel_b).

    Uses store_kg (source_id=None) so the FK constraint on knowledge_relations.source_id
    is satisfied (nullable column).  The local_id→DB-id mapping produced by store_kg
    is captured by fetching the created objects after insertion.
    """
    nb = repo.create_notebook(NotebookCreate(name="conflict-test"))
    repo.store_kg(nb.id, None, [
        {"local_id": "A", "object_type": "claim",
         "payload": {"name": "Claim A", "statement": "A is true"}, "evidence": []},
        {"local_id": "B", "object_type": "claim",
         "payload": {"name": "Claim B", "statement": "B is true"}, "evidence": []},
    ], [
        {"source_local_id": "A", "target_local_id": "B", "edge_type": "supports", "evidence": []},
        {"source_local_id": "B", "target_local_id": "A", "edge_type": "contradicts", "evidence": []},
    ])
    # Fetch DB ids from the stored objects and relations
    with repo._connect() as db:
        objs = db.execute(
            "SELECT id, payload FROM knowledge_objects WHERE notebook_id=? ORDER BY created_at, id",
            (nb.id,),
        ).fetchall()
    import json as _json
    oid_a = next(r["id"] for r in objs if _json.loads(r["payload"]).get("name") == "Claim A")
    oid_b = next(r["id"] for r in objs if _json.loads(r["payload"]).get("name") == "Claim B")
    rels = repo.relations_for_notebook(nb.id)
    assert len(rels) == 2, f"expected 2 relations, got {rels}"
    rel_a = rels[0]["id"]
    rel_b = rels[1]["id"]
    return repo, nb.id, oid_a, oid_b, rel_a, rel_b


# ---------------------------------------------------------------------------
# Helper: read object status directly
# ---------------------------------------------------------------------------

def _get_object_status(repo, notebook_id: str, object_id: str) -> str:
    with repo._connect() as db:
        row = db.execute(
            "SELECT status FROM knowledge_objects WHERE id=? AND notebook_id=?",
            (object_id, notebook_id),
        ).fetchone()
    assert row is not None, f"object {object_id!r} not found"
    return row["status"]


def _get_relation_review_status(repo, notebook_id: str, rel_id: str) -> str:
    with repo._connect() as db:
        row = db.execute(
            "SELECT review_status FROM knowledge_relations WHERE id=? AND notebook_id=?",
            (rel_id, notebook_id),
        ).fetchone()
    assert row is not None, f"relation {rel_id!r} not found"
    return row["review_status"]


def _get_object_payload(repo, notebook_id: str, object_id: str) -> dict:
    with repo._connect() as db:
        row = db.execute(
            "SELECT payload FROM knowledge_objects WHERE id=? AND notebook_id=?",
            (object_id, notebook_id),
        ).fetchone()
    assert row is not None
    return json.loads(row["payload"] or "{}")


# ---------------------------------------------------------------------------
# keep → no-op
# ---------------------------------------------------------------------------

def test_keep_returns_keep_and_changes_nothing(seeded):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    result = repo.apply_conflict_resolution(
        nb_id, kind="node", left_ref=oid_a, right_ref=oid_b, resolution="keep"
    )
    assert result == {"action": "keep"}
    assert _get_object_status(repo, nb_id, oid_a) == "approved"
    assert _get_object_status(repo, nb_id, oid_b) == "approved"


def test_keep_edge_returns_keep(seeded):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    result = repo.apply_conflict_resolution(
        nb_id, kind="edge", left_ref=rel_a, right_ref=rel_b, resolution="keep"
    )
    assert result == {"action": "keep"}
    assert _get_relation_review_status(repo, nb_id, rel_a) == "pending"
    assert _get_relation_review_status(repo, nb_id, rel_b) == "pending"


# ---------------------------------------------------------------------------
# node discard → loser gets status='conflict', winner untouched
# ---------------------------------------------------------------------------

def test_node_discard_loser_gets_conflict_status(seeded):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    result = repo.apply_conflict_resolution(
        nb_id, kind="node", left_ref=oid_a, right_ref=oid_b,
        resolution="discard", winner_ref=oid_a,
    )
    assert result == {"action": "discard", "loser": oid_b}
    assert _get_object_status(repo, nb_id, oid_b) == "conflict"
    assert _get_object_status(repo, nb_id, oid_a) == "approved"   # winner untouched


def test_node_discard_winner_right_loser_is_left(seeded):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    result = repo.apply_conflict_resolution(
        nb_id, kind="node", left_ref=oid_a, right_ref=oid_b,
        resolution="discard", winner_ref=oid_b,
    )
    assert result["loser"] == oid_a
    assert _get_object_status(repo, nb_id, oid_a) == "conflict"
    assert _get_object_status(repo, nb_id, oid_b) == "approved"


# ---------------------------------------------------------------------------
# edge discard → loser relation gets review_status='rejected'
# ---------------------------------------------------------------------------

def test_edge_discard_loser_rejected(seeded):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    result = repo.apply_conflict_resolution(
        nb_id, kind="edge", left_ref=rel_a, right_ref=rel_b,
        resolution="discard", winner_ref=rel_a,
    )
    assert result == {"action": "discard", "loser": rel_b}
    assert _get_relation_review_status(repo, nb_id, rel_b) == "rejected"
    assert _get_relation_review_status(repo, nb_id, rel_a) == "pending"  # winner untouched


def test_edge_discard_winner_right(seeded):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    result = repo.apply_conflict_resolution(
        nb_id, kind="edge", left_ref=rel_a, right_ref=rel_b,
        resolution="discard", winner_ref=rel_b,
    )
    assert result["loser"] == rel_a
    assert _get_relation_review_status(repo, nb_id, rel_a) == "rejected"
    assert _get_relation_review_status(repo, nb_id, rel_b) == "pending"


# ---------------------------------------------------------------------------
# discard with bad/None winner_ref → skipped, nothing changed
# ---------------------------------------------------------------------------

def test_node_discard_none_winner_ref_skipped(seeded):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    result = repo.apply_conflict_resolution(
        nb_id, kind="node", left_ref=oid_a, right_ref=oid_b,
        resolution="discard", winner_ref=None,
    )
    assert result["action"] == "skipped"
    assert "no valid winner_ref" in result["reason"]
    assert _get_object_status(repo, nb_id, oid_a) == "approved"
    assert _get_object_status(repo, nb_id, oid_b) == "approved"


def test_node_discard_bad_winner_ref_skipped(seeded):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    result = repo.apply_conflict_resolution(
        nb_id, kind="node", left_ref=oid_a, right_ref=oid_b,
        resolution="discard", winner_ref="some-other-id",
    )
    assert result["action"] == "skipped"
    assert _get_object_status(repo, nb_id, oid_a) == "approved"
    assert _get_object_status(repo, nb_id, oid_b) == "approved"


def test_edge_discard_none_winner_ref_skipped(seeded):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    result = repo.apply_conflict_resolution(
        nb_id, kind="edge", left_ref=rel_a, right_ref=rel_b,
        resolution="discard", winner_ref=None,
    )
    assert result["action"] == "skipped"
    assert _get_relation_review_status(repo, nb_id, rel_a) == "pending"
    assert _get_relation_review_status(repo, nb_id, rel_b) == "pending"


# ---------------------------------------------------------------------------
# node modify → target object payload updated
# ---------------------------------------------------------------------------

def test_node_modify_updates_target_payload(seeded):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    new_payload = {"name": "Claim A (merged)", "statement": "A is definitively true"}
    result = repo.apply_conflict_resolution(
        nb_id, kind="node", left_ref=oid_a, right_ref=oid_b,
        resolution="modify", winner_ref=oid_a, resolved_payload=new_payload,
    )
    assert result == {"action": "modify", "target": oid_a}
    stored = _get_object_payload(repo, nb_id, oid_a)
    assert stored["name"] == "Claim A (merged)"
    assert stored["statement"] == "A is definitively true"
    # other object untouched
    stored_b = _get_object_payload(repo, nb_id, oid_b)
    assert stored_b["name"] == "Claim B"


def test_node_modify_winner_ref_none_falls_back_to_left(seeded):
    """When winner_ref is None (or not in refs), target falls back to left_ref."""
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    new_payload = {"name": "Fallback modified"}
    result = repo.apply_conflict_resolution(
        nb_id, kind="node", left_ref=oid_a, right_ref=oid_b,
        resolution="modify", winner_ref=None, resolved_payload=new_payload,
    )
    assert result == {"action": "modify", "target": oid_a}
    stored = _get_object_payload(repo, nb_id, oid_a)
    assert stored["name"] == "Fallback modified"


def test_node_modify_winner_ref_unknown_falls_back_to_left(seeded):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    new_payload = {"name": "Unknown winner fallback"}
    result = repo.apply_conflict_resolution(
        nb_id, kind="node", left_ref=oid_a, right_ref=oid_b,
        resolution="modify", winner_ref="not-a-real-id", resolved_payload=new_payload,
    )
    assert result["target"] == oid_a


def test_node_modify_without_payload_skipped(seeded):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    result = repo.apply_conflict_resolution(
        nb_id, kind="node", left_ref=oid_a, right_ref=oid_b,
        resolution="modify", winner_ref=oid_a, resolved_payload=None,
    )
    assert result["action"] == "skipped"
    assert "modify without payload" in result["reason"]
    # nothing changed
    assert _get_object_payload(repo, nb_id, oid_a)["name"] == "Claim A"


def test_node_modify_non_dict_payload_skipped(seeded):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    result = repo.apply_conflict_resolution(
        nb_id, kind="node", left_ref=oid_a, right_ref=oid_b,
        resolution="modify", winner_ref=oid_a, resolved_payload="not a dict",
    )
    assert result["action"] == "skipped"


# ---------------------------------------------------------------------------
# edge modify → no-op (unsupported in v1)
# ---------------------------------------------------------------------------

def test_edge_modify_returns_skipped(seeded):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    result = repo.apply_conflict_resolution(
        nb_id, kind="edge", left_ref=rel_a, right_ref=rel_b,
        resolution="modify", winner_ref=rel_a,
        resolved_payload={"edge_type": "supports"},
    )
    assert result["action"] == "skipped"
    assert "edge modify unsupported" in result["reason"]
    # nothing changed
    assert _get_relation_review_status(repo, nb_id, rel_a) == "pending"
    assert _get_relation_review_status(repo, nb_id, rel_b) == "pending"


# ---------------------------------------------------------------------------
# unknown resolution/kind → ValueError
# ---------------------------------------------------------------------------

def test_unknown_resolution_raises_value_error(seeded):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    with pytest.raises(ValueError, match="resolution"):
        repo.apply_conflict_resolution(
            nb_id, kind="node", left_ref=oid_a, right_ref=oid_b,
            resolution="foobar",
        )


def test_unknown_kind_raises_value_error(seeded):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    with pytest.raises(ValueError, match="kind"):
        repo.apply_conflict_resolution(
            nb_id, kind="graph", left_ref=oid_a, right_ref=oid_b,
            resolution="keep",
        )


# ---------------------------------------------------------------------------
# node modify → pre-existing payload fields NOT in resolved_payload survive
# ---------------------------------------------------------------------------

def test_node_modify_preserves_pre_existing_payload_fields(seeded):
    """Fix 2 regression: apply_conflict_resolution modify must MERGE resolved_payload
    onto the existing payload, not replace it wholesale.

    update_knowledge replaces the entire payload column.  Without the merge step a
    resolved_payload like {"name": "..."} would silently drop pre-existing fields
    (section_path, validity_scope, steps, …).

    This test:
    - Seeds an object whose payload already contains section_path="§2.3".
    - Calls apply_conflict_resolution with resolved_payload={"name": "Updated name"}
      which intentionally omits section_path.
    - Asserts section_path="§2.3" still survives AND name was updated.
    """
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded

    # First, enrich Claim A's payload with an extra field (section_path)
    # that will NOT appear in the adjudicator's resolved_payload.
    import json as _json
    with repo._write() as db:
        db.execute(
            "UPDATE knowledge_objects SET payload=? WHERE id=? AND notebook_id=?",
            (
                _json.dumps({"name": "Claim A", "statement": "A is true", "section_path": "§2.3"}),
                oid_a,
                nb_id,
            ),
        )

    # Adjudicator only returns a modified name — section_path is absent
    resolved_payload = {"name": "Claim A (reconciled)"}

    result = repo.apply_conflict_resolution(
        nb_id, kind="node", left_ref=oid_a, right_ref=oid_b,
        resolution="modify", winner_ref=oid_a, resolved_payload=resolved_payload,
    )
    assert result == {"action": "modify", "target": oid_a}

    stored = _get_object_payload(repo, nb_id, oid_a)
    # Modified field must be updated
    assert stored["name"] == "Claim A (reconciled)", (
        f"Expected name to be updated, got {stored!r}"
    )
    # Pre-existing field must survive
    assert stored.get("section_path") == "§2.3", (
        f"section_path was dropped — modify replaced rather than merged the payload. "
        f"Got {stored!r}"
    )
    # Other original field not in resolved_payload must also survive
    assert stored.get("statement") == "A is true", (
        f"statement was dropped unexpectedly. Got {stored!r}"
    )
