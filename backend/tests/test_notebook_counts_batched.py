"""C5: _notebook_from_row's 7 per-notebook COUNT(*) queries (sources + 6
per-object_type knowledge counts) reworked to 2 (sources + one GROUP BY
object_type query) — was effectively N+1 across list_notebooks (7 queries *
N notebooks). Equivalence oracle vs the old per-type COUNT(*) implementation,
plus a query-count spy proving the batched path.
"""
import json

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, USABLE_STATUSES
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _seed_notebook_with_knowledge(repo, name, type_counts: dict):
    nb = repo.create_notebook(NotebookCreate(name=name))
    for object_type, n in type_counts.items():
        for i in range(n):
            repo._test_insert_object(nb.id, object_type, {"name": f"{object_type}-{i}"})
    return nb


def _oracle_notebook_summary(repo, notebook_id):
    """Verbatim pre-fix computation: 6 separate _count_knowledge calls."""
    with repo._connect() as db:
        row = db.execute("SELECT * FROM notebooks WHERE id=?", (notebook_id,)).fetchone()
        counts = {
            "sources": repo._count(db, "sources", "notebook_id", row["id"]),
            "rules": repo._count_knowledge(db, row["id"], "rule"),
            "cases": repo._count_knowledge(db, row["id"], "case"),
            "checklist_items": repo._count_knowledge(db, row["id"], "checklist"),
            "methods": repo._count_knowledge(db, row["id"], "method"),
            "risks": repo._count_knowledge(db, row["id"], "risk"),
            "glossary": repo._count_knowledge(db, row["id"], "glossary"),
        }
    return counts


def test_notebook_counts_equal_oracle(repo):
    nb = _seed_notebook_with_knowledge(repo, "kb", {
        "rule": 2, "case": 3, "checklist": 1, "method": 0, "risk": 4, "glossary": 5,
    })
    summary = repo.get_notebook(nb.id)
    oracle = _oracle_notebook_summary(repo, nb.id)
    assert summary.counts == oracle
    assert summary.counts == {
        "sources": 0, "rules": 2, "cases": 3, "checklist_items": 1,
        "methods": 0, "risks": 4, "glossary": 5,
    }


def test_notebook_counts_zero_type_defaults_to_zero(repo):
    """A notebook with knowledge objects of only SOME types must still report
    0 (not KeyError/missing) for absent types — the GROUP BY result only has
    rows for types actually present."""
    nb = _seed_notebook_with_knowledge(repo, "kb-sparse", {"rule": 1})
    summary = repo.get_notebook(nb.id)
    assert summary.counts["rules"] == 1
    for key in ("cases", "checklist_items", "methods", "risks", "glossary"):
        assert summary.counts[key] == 0


def test_notebook_counts_respects_usable_statuses(repo):
    """Only USABLE_STATUSES objects count — a deprecated object must not be
    counted, matching the old per-type _count_knowledge filter."""
    nb = repo.create_notebook(NotebookCreate(name="kb-status"))
    oid = repo._test_insert_object(nb.id, "rule", {"name": "will be deprecated"})
    with repo._write() as db:
        db.execute("UPDATE knowledge_objects SET status='deprecated' WHERE id=?", (oid,))
    summary = repo.get_notebook(nb.id)
    assert summary.counts["rules"] == 0
    assert "deprecated" not in USABLE_STATUSES  # sanity: fixture assumption holds


def test_list_notebooks_batches_knowledge_type_counts_not_n_plus_1(repo, monkeypatch):
    """list_notebooks over N notebooks must issue exactly 2 knowledge-count
    queries PER notebook (1 sources COUNT + 1 GROUP BY object_type), not 7 —
    spy on the connection boundary for the specific query shapes."""
    for i in range(6):
        _seed_notebook_with_knowledge(repo, f"kb-{i}", {"rule": 1, "case": 1})

    calls = {"group_by": 0, "per_type_count": 0}
    orig_connect = repo._connect

    class _SpyConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *a, **kw):
            if "GROUP BY object_type" in sql:
                calls["group_by"] += 1
            elif "FROM knowledge_objects" in sql and "object_type = ?" in sql:
                calls["per_type_count"] += 1
            return self._inner.execute(sql, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

    monkeypatch.setattr(repo, "_connect", lambda: _SpyConn(orig_connect()))
    notebooks = repo.list_notebooks()
    assert len(notebooks) == 6
    assert calls["per_type_count"] == 0, (
        "expected zero per-object_type COUNT(*) calls — batched via GROUP BY")
    assert calls["group_by"] == 6, (
        f"expected exactly 1 GROUP BY query per notebook (6 notebooks), got {calls['group_by']}")
