import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, _now
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


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
    monkeypatch.setattr(repo._runtime.source_ingestion, "run_extraction", lambda sid: calls.append(sid))
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
    monkeypatch.setattr(repo._runtime.source_ingestion, "run_extraction", lambda sid: calls.append(sid))
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


# ---------------------------------------------------------------------------
# P4-6: 严格推理门控(kg_required) + federated_retrieve 接通
# ---------------------------------------------------------------------------

from app.models.schemas import AskRequest


def test_strict_blocked_when_no_kg_no_base(repo):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    resp = repo.ask_reasoning(nb.id, AskRequest(question="q", mode="reasoning"))
    assert resp.kg_required is True
    assert resp.reasoning_trace is None       # did NOT run the agentic loop


def test_strict_allowed_when_base_has_kg(repo):
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    repo.store_kg(base.id, None, [
        {"local_id": "B1", "object_type": "concept",
         "payload": {"name": "Engram"}, "evidence": []}], [])
    empty = repo.create_notebook(NotebookCreate(name="empty"))   # own KG empty
    resp = repo.ask_reasoning(empty.id, AskRequest(question="Engram", mode="reasoning"))
    assert resp.kg_required is False           # base satisfies the gate


def test_reasoning_search_federates_base(repo):
    from app.services.reasoning_retrieval import ReasoningRetriever
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    repo.store_kg(base.id, None, [
        {"local_id": "B1", "object_type": "concept",
         "payload": {"name": "Engram"}, "evidence": []}], [])
    empty = repo.create_notebook(NotebookCreate(name="empty"))
    hits = ReasoningRetriever.from_repository(repo, repo.settings).search(empty.id, "Engram")
    names = {h.payload.get("name") for h in hits}
    assert "Engram" in names                   # base hit surfaced via federation


# ---------------------------------------------------------------------------
# Citation.tier: reasoning-mode citations must reflect the federated hit's
# tier, not silently default to personal. 真机 bug:DeepSeek-V4 一次 reasoning
# 问答 12 条 citations 里 8 条来自 base 库,前端徽章却只见 personal——根因是
# _citations_from/_citation 完全不读 RetrievedKnowledge.tier(federated_retrieve
# 早就打好了标)。这里直接跑 federated_retrieve → _citations_from(与
# ask_reasoning 12118-12120 同一调用形状),不依赖 LLM 配置。
# ---------------------------------------------------------------------------

_BASE_EVIDENCE = [{"source_id": "src-base", "source_title": "BaseDoc",
                   "element_id": "el-base-0001", "element_type": "paragraph",
                   "location_label": "1", "quoted_span": "Engram is a memory trace",
                   "confidence": 1.0}]
_PERSONAL_EVIDENCE = [{"source_id": "src-own", "source_title": "OwnDoc",
                       "element_id": "el-own-0001", "element_type": "paragraph",
                       "location_label": "1", "quoted_span": "my own note on Engram",
                       "confidence": 1.0}]


def test_citations_from_reasoning_hits_carries_base_tier(repo):
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    repo.store_kg(base.id, None, [
        {"local_id": "B1", "object_type": "concept",
         "payload": {"name": "Engram"}, "evidence": _BASE_EVIDENCE}], [])
    active = repo.create_notebook(NotebookCreate(name="active"))
    repo.store_kg(active.id, None, [
        {"local_id": "A1", "object_type": "concept",
         "payload": {"name": "Engram encoding"}, "evidence": _PERSONAL_EVIDENCE}], [])

    # Mirrors ask_reasoning's own call shape (sqlite_repository.py ~12118-12120):
    # top_hits federated across base ⊕ active, then bound to Citation objects.
    top_hits = repo.retrieval.federated_retrieve(active.id, "Engram")
    cited_element_ids = {ev.element_id for item in top_hits for ev in item.evidence}
    citations = repo._citations_from(top_hits, cited_element_ids, "KG evidence")

    tier_by_source = {c.source_id: c.tier for c in citations}
    assert tier_by_source.get("src-base") == "base", (
        f"base 库命中的 citation.tier 应为 base,实为 {tier_by_source.get('src-base')} "
        f"(全部 citations: {[(c.source_id, c.tier) for c in citations]})")
    assert tier_by_source.get("src-own") == "personal", (
        f"active 库自己命中的 citation.tier 应为 personal,实为 {tier_by_source.get('src-own')}")


def test_tier_map_for_batches_and_defaults_unknown_to_personal(repo):
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    personal = repo.create_notebook(NotebookCreate(name="personal"))

    tier_map = repo._tier_map_for({base.id, personal.id, "nonexistent-nb-id"})

    assert tier_map[base.id] == "base"
    assert tier_map[personal.id] == "personal"
    # Unknown ids are simply absent (caller's .get(..., "personal") supplies the
    # safe default) — _tier_map_for itself only returns rows it actually found.
    assert "nonexistent-nb-id" not in tier_map


def test_tier_map_for_empty_input_returns_empty_dict_without_querying(repo):
    assert repo._tier_map_for(set()) == {}
    assert repo._tier_map_for([]) == {}
