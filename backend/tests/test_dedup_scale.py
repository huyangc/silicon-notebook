"""find_duplicates (查重) must scale: block by normalized seed (O(N + Σblock²))
instead of the old O(N²) all-pairs that loaded EVERY element vector and froze the
知识库 page at 10^5+ objects."""
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings(_env_file=None))
    r.embedder = FakeEmbedder(dim=16)
    return r


def _c(lid, name):
    return {"local_id": lid, "object_type": "concept", "payload": {"name": name},
            "evidence": [{"quoted_span": "q", "element_id": f"el-{lid}", "source_id": "s",
                          "source_title": "D", "element_type": "paragraph",
                          "location_label": "1", "confidence": 1.0}]}


def _no_gather(repo, monkeypatch):
    # Regression guard: the scaled path must NOT gather all elements/vectors.
    def _boom(*a, **k):
        raise AssertionError("find_duplicates must not gather all elements/vectors")
    monkeypatch.setattr(repo.retrieval.candidates, "_gather_elements", _boom)


def test_find_duplicates_groups_by_normalized_seed(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, "s1",
                  [_c("a", "MOSFET"), _c("b", "mosfet"), _c("c", "Cascode")], [])
    _no_gather(repo, monkeypatch)
    groups = repo.find_duplicates(nb.id, "concept")
    # MOSFET/mosfet share a normalized seed → one group of 2; Cascode is a singleton.
    assert len(groups) == 1
    assert len(groups[0].members) == 2
    assert {m.headline.lower() for m in groups[0].members} == {"mosfet"}


def test_find_duplicates_empty_when_all_distinct(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, "s1",
                  [_c("a", "MOSFET"), _c("b", "Cascode"), _c("c", "BJT")], [])
    _no_gather(repo, monkeypatch)
    assert repo.find_duplicates(nb.id, "concept") == []


def test_find_duplicates_deterministic_order(repo, monkeypatch):
    # larger groups first, deterministic across calls
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, "s1", [
        _c("a", "MOSFET"), _c("b", "mosfet"), _c("c", "MOSFET "),  # 3-member seed
        _c("d", "cascode"), _c("e", "Cascode"),                    # 2-member seed
    ], [])
    _no_gather(repo, monkeypatch)
    g1 = repo.find_duplicates(nb.id, "concept")
    g2 = repo.find_duplicates(nb.id, "concept")
    assert [len(g.members) for g in g1] == [3, 2]        # largest first
    assert [[m.id for m in g.members] for g in g1] == \
           [[m.id for m in g.members] for g in g2]       # stable
