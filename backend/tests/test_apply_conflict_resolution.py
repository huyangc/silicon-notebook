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
from tests.model_testkit import bind_all_embedding_clients


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
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
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
    result = repo._runtime.knowledge_governance.apply_conflict_resolution(
        nb_id, kind="node", left_ref=oid_a, right_ref=oid_b, resolution="keep"
    )
    assert result == {"action": "keep"}
    assert _get_object_status(repo, nb_id, oid_a) == "approved"
    assert _get_object_status(repo, nb_id, oid_b) == "approved"


def test_keep_edge_returns_keep(seeded):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    result = repo._runtime.knowledge_governance.apply_conflict_resolution(
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
    result = repo._runtime.knowledge_governance.apply_conflict_resolution(
        nb_id, kind="node", left_ref=oid_a, right_ref=oid_b,
        resolution="discard", winner_ref=oid_a,
    )
    assert result == {"action": "discard", "loser": oid_b}
    assert _get_object_status(repo, nb_id, oid_b) == "conflict"
    assert _get_object_status(repo, nb_id, oid_a) == "approved"   # winner untouched


def test_node_discard_winner_right_loser_is_left(seeded):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    result = repo._runtime.knowledge_governance.apply_conflict_resolution(
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
    result = repo._runtime.knowledge_governance.apply_conflict_resolution(
        nb_id, kind="edge", left_ref=rel_a, right_ref=rel_b,
        resolution="discard", winner_ref=rel_a,
    )
    assert result == {"action": "discard", "loser": rel_b}
    assert _get_relation_review_status(repo, nb_id, rel_b) == "rejected"
    assert _get_relation_review_status(repo, nb_id, rel_a) == "pending"  # winner untouched


def test_edge_discard_winner_right(seeded):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    result = repo._runtime.knowledge_governance.apply_conflict_resolution(
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
    result = repo._runtime.knowledge_governance.apply_conflict_resolution(
        nb_id, kind="node", left_ref=oid_a, right_ref=oid_b,
        resolution="discard", winner_ref=None,
    )
    assert result["action"] == "skipped"
    assert "no valid winner_ref" in result["reason"]
    assert _get_object_status(repo, nb_id, oid_a) == "approved"
    assert _get_object_status(repo, nb_id, oid_b) == "approved"


def test_node_discard_bad_winner_ref_skipped(seeded):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    result = repo._runtime.knowledge_governance.apply_conflict_resolution(
        nb_id, kind="node", left_ref=oid_a, right_ref=oid_b,
        resolution="discard", winner_ref="some-other-id",
    )
    assert result["action"] == "skipped"
    assert _get_object_status(repo, nb_id, oid_a) == "approved"
    assert _get_object_status(repo, nb_id, oid_b) == "approved"


def test_edge_discard_none_winner_ref_skipped(seeded):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    result = repo._runtime.knowledge_governance.apply_conflict_resolution(
        nb_id, kind="edge", left_ref=rel_a, right_ref=rel_b,
        resolution="discard", winner_ref=None,
    )
    assert result["action"] == "skipped"
    assert _get_relation_review_status(repo, nb_id, rel_a) == "pending"
    assert _get_relation_review_status(repo, nb_id, rel_b) == "pending"


# ---------------------------------------------------------------------------
# node modify → target object payload updated
# ---------------------------------------------------------------------------

def test_node_modify_updates_target_name_only(seeded):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    result = repo._runtime.knowledge_governance.apply_conflict_resolution(
        nb_id, kind="node", left_ref=oid_a, right_ref=oid_b,
        resolution="modify", winner_ref=oid_a, resolved_name=" Claim A (merged) ",
    )
    assert result == {"action": "modify", "target": oid_a}
    stored = _get_object_payload(repo, nb_id, oid_a)
    assert stored["name"] == "Claim A (merged)"
    assert stored["statement"] == "A is true"
    # other object untouched
    stored_b = _get_object_payload(repo, nb_id, oid_b)
    assert stored_b["name"] == "Claim B"


@pytest.mark.parametrize("winner_ref", [None, "not-a-real-id"])
def test_node_modify_without_valid_winner_is_skipped(seeded, winner_ref):
    """resolved_name describes one specific side, so a modify that does not
    name which side is skipped rather than rewriting left_ref by guess."""
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    result = repo._runtime.knowledge_governance.apply_conflict_resolution(
        nb_id, kind="node", left_ref=oid_a, right_ref=oid_b,
        resolution="modify", winner_ref=winner_ref, resolved_name="Guessed side",
    )
    assert result == {"action": "skipped", "reason": "no valid winner_ref for modify"}
    assert _get_object_payload(repo, nb_id, oid_a)["name"] == "Claim A"
    assert _get_object_payload(repo, nb_id, oid_b)["name"] == "Claim B"


@pytest.mark.parametrize("resolved_name", [None, "", "   "])
def test_node_modify_without_name_skipped(seeded, resolved_name):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    result = repo._runtime.knowledge_governance.apply_conflict_resolution(
        nb_id, kind="node", left_ref=oid_a, right_ref=oid_b,
        resolution="modify", winner_ref=oid_a, resolved_name=resolved_name,
    )
    assert result["action"] == "skipped"
    assert "modify without payload" in result["reason"]
    # nothing changed
    assert _get_object_payload(repo, nb_id, oid_a)["name"] == "Claim A"


def test_node_modify_non_string_name_skipped(seeded):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    result = repo._runtime.knowledge_governance.apply_conflict_resolution(
        nb_id, kind="node", left_ref=oid_a, right_ref=oid_b,
        resolution="modify", winner_ref=oid_a, resolved_name={"name": "x"},
    )
    assert result["action"] == "skipped"
    assert _get_object_payload(repo, nb_id, oid_a)["name"] == "Claim A"


# ---------------------------------------------------------------------------
# edge modify → no-op (unsupported in v1)
# ---------------------------------------------------------------------------

def test_edge_modify_returns_skipped(seeded):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    result = repo._runtime.knowledge_governance.apply_conflict_resolution(
        nb_id, kind="edge", left_ref=rel_a, right_ref=rel_b,
        resolution="modify", winner_ref=rel_a,
        resolved_name="A supports B",
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
        repo._runtime.knowledge_governance.apply_conflict_resolution(
            nb_id, kind="node", left_ref=oid_a, right_ref=oid_b,
            resolution="foobar",
        )


def test_unknown_kind_raises_value_error(seeded):
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    with pytest.raises(ValueError, match="kind"):
        repo._runtime.knowledge_governance.apply_conflict_resolution(
            nb_id, kind="graph", left_ref=oid_a, right_ref=oid_b,
            resolution="keep",
        )


# ---------------------------------------------------------------------------
# node modify → only the name changes; every other payload field survives
# ---------------------------------------------------------------------------

def test_node_modify_preserves_pre_existing_payload_fields(seeded):
    """Fix 2 regression: a modify rewrites only ``name`` on the existing
    payload, never replaces it wholesale.

    update_knowledge replaces the entire payload column.  Without the merge step a
    rewritten name would silently drop pre-existing fields
    (section_path, validity_scope, steps, …).

    This test:
    - Seeds an object whose payload already contains section_path="§2.3".
    - Calls apply_conflict_resolution with resolved_name="Claim A (reconciled)".
    - Asserts section_path="§2.3" still survives AND name was updated.
    """
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded

    # First, enrich Claim A's payload with an extra field (section_path).
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

    result = repo._runtime.knowledge_governance.apply_conflict_resolution(
        nb_id, kind="node", left_ref=oid_a, right_ref=oid_b,
        resolution="modify", winner_ref=oid_a, resolved_name="Claim A (reconciled)",
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
    # Other original field must also survive
    assert stored.get("statement") == "A is true", (
        f"statement was dropped unexpectedly. Got {stored!r}"
    )


# ---------------------------------------------------------------------------
# stored candidates written under the older arbitrary-object contract
# ---------------------------------------------------------------------------

def test_confirm_legacy_stored_payload_applies_only_its_name(seeded):
    """A pending row adjudicated before the name-only contract may carry any
    keys in resolved_payload; confirming it rewrites the name and ignores
    the rest. A legacy row without a usable name cannot be applied and stays
    pending for review."""
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    governance = repo._runtime.knowledge_governance
    legacy = governance.write_conflict_candidate(
        nb_id, "node", oid_a, oid_b, conflict_type="temporal",
        resolution="modify", winner_ref=oid_b,
        resolved_payload=json.dumps({"name": "Claim B [2000]", "statement": "rewritten"}),
    )
    result = governance.confirm_conflict(nb_id, legacy)
    assert result["action"] == "modify" and result["target"] == oid_b
    stored = _get_object_payload(repo, nb_id, oid_b)
    assert stored == {"name": "Claim B [2000]", "statement": "B is true"}

    nameless = governance.write_conflict_candidate(
        nb_id, "node", oid_a, oid_b, conflict_type="granularity",
        resolution="modify", winner_ref=oid_a,
        resolved_payload=json.dumps({"annotation": "granularity: A ⊂ B"}),
    )
    with pytest.raises(ValueError, match="modify without payload"):
        governance.confirm_conflict(nb_id, nameless)
    assert governance.get_conflict_candidate(nb_id, nameless)["status"] == "pending"
    assert _get_object_payload(repo, nb_id, oid_a)["name"] == "Claim A"


def test_confirm_a_modify_without_a_valid_side_keeps_it_pending(seeded):
    """A row the old prompt adjudicated without winner_ref: applying it would
    change nothing, so it must not leave the review queue as applied."""
    repo, nb_id, oid_a, oid_b, rel_a, rel_b = seeded
    governance = repo._runtime.knowledge_governance
    candidate = governance.write_conflict_candidate(
        nb_id, "node", oid_a, oid_b, conflict_type="temporal",
        resolution="modify", winner_ref=None,
        resolved_payload=json.dumps({"name": "Claim A [2000]"}),
    )
    with pytest.raises(ValueError, match="no valid winner_ref for modify"):
        governance.confirm_conflict(nb_id, candidate)
    assert governance.get_conflict_candidate(nb_id, candidate)["status"] == "pending"
    assert _get_object_payload(repo, nb_id, oid_a)["name"] == "Claim A"


def test_stored_resolved_name_reads_text_objects_and_junk():
    from app.services.knowledge_governance import _stored_resolved_name

    assert _stored_resolved_name('{"name": " X "}') == "X"
    assert _stored_resolved_name({"name": "Y", "other": 1}) == "Y"
    for junk in (None, "", "not json", "[]", '{"name": ""}', '{"name": 3}', {"x": "y"}):
        assert _stored_resolved_name(junk) is None, junk
