"""Governance receipt scopes must not commit ahead of their owning service."""
import json

import pytest

from app.models.schemas import KnowledgeUpdate, NotebookCreate
from tests.test_kg_relink_repository import repo as repo


def test_dirty_failure_rolls_back_object_edit_and_receipt(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="atomic receipt edit")).id
    oid = repo._runtime.knowledge_query.insert_test_object(nb, "claim", {"name": "before"})
    with repo._connect() as db:
        before = dict(db.execute("SELECT * FROM knowledge_objects WHERE id=?", (oid,)).fetchone())
        receipts = [dict(row) for row in db.execute("SELECT * FROM kg_relink_work WHERE notebook_id=?", (nb,))]

    def fail_after_edit(db, notebook_id):
        assert db.in_transaction
        row = db.execute("SELECT payload FROM knowledge_objects WHERE id=?", (oid,)).fetchone()
        assert json.loads(row["payload"])["name"] == "after"
        assert db.execute("SELECT 1 FROM kg_relink_work WHERE notebook_id=? AND status='pending'", (nb,)).fetchone()
        raise RuntimeError("injected dirty-write failure")

    monkeypatch.setattr(repo._runtime.knowledge_governance, "_mark_unified_kg_dirty_in_tx", fail_after_edit)
    with pytest.raises(RuntimeError, match="injected dirty-write failure"):
        repo.update_knowledge(nb, oid, KnowledgeUpdate(payload={"name": "after"}))
    with repo._connect() as db:
        assert dict(db.execute("SELECT * FROM knowledge_objects WHERE id=?", (oid,)).fetchone()) == before
        assert [dict(row) for row in db.execute("SELECT * FROM kg_relink_work WHERE notebook_id=?", (nb,))] == receipts
