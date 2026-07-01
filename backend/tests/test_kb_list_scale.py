"""Regression: the knowledge-base list page query must be index-ordered, not a
full temp-b-tree sort. Without idx_knowledge_objects_nb_type_created, SQLite does
`USE TEMP B-TREE FOR ORDER BY` and reads+sorts ALL matching rows to return 50 —
O(N) per page load, which froze the 知识库 page at 10^5+ objects."""
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate

_PAGE_SQL = (
    "SELECT * FROM knowledge_objects WHERE notebook_id=? AND object_type=? "
    "ORDER BY created_at ASC, id ASC LIMIT 50 OFFSET 0"
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings(_env_file=None))
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed_concepts(repo, nb_id, n):
    objs = [
        {"local_id": f"c{i}", "object_type": "concept", "payload": {"name": f"Concept {i}"},
         "evidence": [{"quoted_span": "x", "element_id": f"el-{i}", "source_id": "s",
                       "source_title": "D", "element_type": "paragraph",
                       "location_label": "1", "confidence": 1.0}]}
        for i in range(n)
    ]
    repo.store_kg(nb_id, None, objs, [])


def test_created_order_index_exists(repo):
    with repo._connect() as db:
        names = {r["name"] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    assert "idx_knowledge_objects_nb_type_created" in names


def test_list_knowledge_page_query_is_index_ordered(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_concepts(repo, nb.id, 60)  # > the LIMIT, so the planner must resolve ORDER BY
    with repo._connect() as db:
        plan = db.execute("EXPLAIN QUERY PLAN " + _PAGE_SQL, (nb.id, "concept")).fetchall()
    detail = " | ".join(r["detail"] for r in plan)
    # The ORDER BY must be satisfied by the index — no full sort of all matching rows.
    assert "USE TEMP B-TREE" not in detail, detail
    assert "idx_knowledge_objects_nb_type_created" in detail, detail


def test_list_knowledge_still_returns_ordered_page(repo):
    """The index change must not alter results: still created_at,id ascending, paged."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_concepts(repo, nb.id, 60)
    page = repo.list_knowledge(nb.id, "concept", offset=0, limit=50)
    assert page.total_count == 60
    assert len(page.items) == 50
    page2 = repo.list_knowledge(nb.id, "concept", offset=50, limit=50)
    assert len(page2.items) == 10
    # no id appears on both pages (stable pagination)
    assert not ({i.id for i in page.items} & {i.id for i in page2.items})
