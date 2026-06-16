import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, _now
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


def test_kg_auto_extract_defaults_false():
    assert Settings().kg_auto_extract is False


def test_kg_auto_extract_env_override(monkeypatch):
    monkeypatch.setenv("KG_AUTO_EXTRACT", "true")
    assert Settings().kg_auto_extract is True


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed_concept(repo, nb_id, local_id="K1", name="Engram"):
    repo.store_kg(nb_id, None, [
        {"local_id": local_id, "object_type": "concept",
         "payload": {"name": name, "section_path": "1"}, "evidence": []},
    ], [])


def test_notebook_has_kg(repo):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    assert repo._notebook_has_kg(nb.id) is False
    _seed_concept(repo, nb.id)
    assert repo._notebook_has_kg(nb.id) is True


def test_should_extract_false_when_auto_off_and_no_kg(repo):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    assert repo._should_extract_kg(nb.id) is False


def test_should_extract_true_when_auto_on(repo, monkeypatch):
    monkeypatch.setattr(repo.settings, "kg_auto_extract", True)
    nb = repo.create_notebook(NotebookCreate(name="n"))
    assert repo._should_extract_kg(nb.id) is True


def test_should_extract_true_when_notebook_already_has_kg(repo):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    _seed_concept(repo, nb.id)
    assert repo._should_extract_kg(nb.id) is True


# ---------------------------------------------------------------------------
# P4-3: build_notebook_kg
# ---------------------------------------------------------------------------

def _make_source(repo, nb_id, sid, status="extracted"):
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES (?,?,?,'markdown',?,'parsed','a.md','',0,'','','academic_paper',?,?)",
            (sid, nb_id, "Doc", status, _now(), _now()))
    return sid


def _configure_llm(repo):
    repo.llm_client = type("C", (), {"configured": True})()


def test_build_notebook_kg_runs_extraction_per_kgless_source(repo, monkeypatch):
    _configure_llm(repo)
    nb = repo.create_notebook(NotebookCreate(name="n"))
    s1 = _make_source(repo, nb.id, "s1")
    s2 = _make_source(repo, nb.id, "s2")
    calls = []
    monkeypatch.setattr(repo, "_run_extraction", lambda sid: calls.append(sid))
    repo.build_notebook_kg(nb.id)
    assert set(calls) == {"s1", "s2"}


def test_build_notebook_kg_skips_sources_with_kg(repo, monkeypatch):
    _configure_llm(repo)
    nb = repo.create_notebook(NotebookCreate(name="n"))
    s1 = _make_source(repo, nb.id, "s1")
    repo.store_kg(nb.id, "s1", [
        {"local_id": "K1", "object_type": "concept",
         "payload": {"name": "X"}, "evidence": []}], [])   # s1 already has KG
    s2 = _make_source(repo, nb.id, "s2")
    calls = []
    monkeypatch.setattr(repo, "_run_extraction", lambda sid: calls.append(sid))
    repo.build_notebook_kg(nb.id)
    assert calls == ["s2"]                                  # idempotent: skip s1


def test_build_notebook_kg_requires_llm(repo):
    repo.llm_client = type("C", (), {"configured": False})()
    nb = repo.create_notebook(NotebookCreate(name="n"))
    with pytest.raises(RuntimeError):
        repo.build_notebook_kg(nb.id)


# ---------------------------------------------------------------------------
# P4-4: base_kg_available signal
# ---------------------------------------------------------------------------

def test_any_base_notebook_has_kg(repo):
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    assert repo._any_base_notebook_has_kg() is False
    # personal notebook with KG must NOT count as base:
    pers = repo.create_notebook(NotebookCreate(name="p"))
    repo.store_kg(pers.id, None, [
        {"local_id": "P1", "object_type": "concept",
         "payload": {"name": "P"}, "evidence": []}], [])
    assert repo._any_base_notebook_has_kg() is False
    # now give the BASE notebook KG:
    repo.store_kg(base.id, None, [
        {"local_id": "B1", "object_type": "concept",
         "payload": {"name": "B"}, "evidence": []}], [])
    assert repo._any_base_notebook_has_kg() is True


def test_base_kg_available_on_notebook_summary(repo):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    assert repo.get_notebook(nb.id).base_kg_available is False
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    repo.store_kg(base.id, None, [
        {"local_id": "B1", "object_type": "concept",
         "payload": {"name": "B"}, "evidence": []}], [])
    assert repo.get_notebook(nb.id).base_kg_available is True


# ---------------------------------------------------------------------------
# P4-5: 退役 fast/global 模式
# ---------------------------------------------------------------------------

from app.services.ask_modes import resolve_mode, UnknownAskMode, ASK_MODES


def test_fast_global_removed_from_registry():
    assert "fast" not in ASK_MODES
    assert "global" not in ASK_MODES


def test_retired_modes_alias_to_chunk():
    assert resolve_mode("fast").id == "chunk"
    assert resolve_mode("global").id == "chunk"


def test_strict_modes_and_default_intact():
    assert resolve_mode("reasoning").id == "reasoning"
    assert resolve_mode("graph").id == "graph"
    assert resolve_mode(None).id == "chunk"


def test_unknown_mode_still_raises():
    with pytest.raises(UnknownAskMode):
        resolve_mode("bogus")
