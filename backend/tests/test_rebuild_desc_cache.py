"""Tests for the incremental cache + parallelization of concept-description
generation in rebuild_unified_kg (perf fix).

The description path only fires for multi-member (cross-doc merged) canonicals.
We build that by storing the SAME normalized concept name from two "sources",
each carrying evidence with a quoted_span, so the rebuild fuses them into one
canonical with 2 members and asks the (fake) LLM for a description.
"""
import json
import threading
import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository, _concept_desc_sig


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")  # embedder_configured=True
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)  # inject; no real model loads (lazy)
    return r


class _DescLLM:
    """Fake KG LLM: counts description calls and returns a distinct description
    per concept name so we can assert which cluster got which description.
    Thread-safe counter because PHASE 2 runs chat_json concurrently."""

    configured = True

    def __init__(self):
        self.calls = 0
        self.names_seen = []
        self._lock = threading.Lock()

    def chat_json(self, messages, schema):
        content = messages[0]["content"]
        # concept_description_prompt embeds "Concept: <name>\n"
        name = ""
        for line in content.splitlines():
            if line.startswith("Concept: "):
                name = line[len("Concept: "):].strip()
                break
        with self._lock:
            self.calls += 1
            self.names_seen.append(name)
        return json.dumps({"description": f"desc-for::{name}"})


def _concept_with_evidence(local_id, name, span, source_title="D"):
    return {
        "local_id": local_id,
        "object_type": "concept",
        "payload": {"name": name, "section_path": "1"},
        "evidence": [{
            "source_id": "s", "source_title": source_title,
            "element_id": "e", "element_type": "p", "location_label": "1",
            "quoted_span": span, "confidence": 1.0,
        }],
    }


def _make_merged_notebook(repo, name="MOSFET", spans=("span-a", "span-b")):
    """Create a notebook whose two sources share a normalized concept name so
    they fuse into ONE canonical with 2 members (fires the description path)."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, "s1", [_concept_with_evidence("a", name, spans[0])], [])
    repo.store_kg(nb.id, "s2", [_concept_with_evidence("b", name.lower(), spans[1])], [])
    return nb


def _cluster_descs(repo, nb_id):
    """canonical_id -> (canonical_description, canonical_desc_sig) for concepts."""
    with repo._connect() as db:
        rows = db.execute(
            "SELECT DISTINCT canonical_id, canonical_description, canonical_desc_sig "
            "FROM concept_clusters WHERE notebook_id=? AND object_type='concept'",
            (nb_id,)).fetchall()
    return {r["canonical_id"]: (r["canonical_description"], r["canonical_desc_sig"]) for r in rows}


# --- 1. signature purity ----------------------------------------------------

def test_concept_desc_sig_deterministic_and_order_insensitive():
    # order-insensitive (we sort the quote set before hashing)
    assert _concept_desc_sig("N", ["a", "b"]) == _concept_desc_sig("N", ["b", "a"])
    # deterministic (same inputs -> same sig)
    assert _concept_desc_sig("N", ["a", "b"]) == _concept_desc_sig("N", ["a", "b"])
    # changing a quote changes the sig
    assert _concept_desc_sig("N", ["a", "b"]) != _concept_desc_sig("N", ["a", "c"])
    # changing the name changes the sig
    assert _concept_desc_sig("N", ["a", "b"]) != _concept_desc_sig("M", ["a", "b"])


# --- 6. migration -----------------------------------------------------------

def test_migration_adds_canonical_desc_sig_column(repo):
    with repo._connect() as db:
        cols = {r["name"] for r in db.execute(
            "PRAGMA table_info(concept_clusters)").fetchall()}
    assert "canonical_desc_sig" in cols


# --- 4. parallel correctness / behavior preserved ---------------------------

def test_all_multimember_canonicals_get_descriptions(repo):
    llm = _DescLLM()
    repo.llm_client = llm  # -> kg_llm_client resolves to this fake
    # two independent merged concepts (each 2 members across sources)
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, "s1", [
        _concept_with_evidence("a1", "MOSFET", "mosfet is a transistor"),
        _concept_with_evidence("b1", "cascode", "cascode boosts rout"),
    ], [])
    repo.store_kg(nb.id, "s2", [
        _concept_with_evidence("a2", "mosfet", "the MOSFET conducts"),
        _concept_with_evidence("b2", "Cascode", "cascode stage"),
    ], [])
    repo.rebuild_unified_kg(nb.id)

    descs = _cluster_descs(repo, nb.id)
    # both canonicals populated with the fake's per-name description
    populated = {d for (d, _sig) in descs.values() if d}
    assert "desc-for::MOSFET" in populated
    assert "desc-for::cascode" in populated
    assert llm.calls == 2   # exactly one LLM call per multi-member canonical
    # sigs persisted non-empty for the described canonicals
    assert all(sig for (d, sig) in descs.values() if d)


# --- 2. cache reuse ---------------------------------------------------------

def test_rebuild_reuses_cached_descriptions(repo):
    llm = _DescLLM()
    repo.llm_client = llm
    nb = _make_merged_notebook(repo)

    repo.rebuild_unified_kg(nb.id)
    first_calls = llm.calls
    assert first_calls >= 1
    before = _cluster_descs(repo, nb.id)
    assert any(d for (d, _s) in before.values())          # description persisted
    assert all(sig for (d, sig) in before.values() if d)   # with a non-empty sig

    # Rebuild again with NO changes -> zero new description calls, values preserved
    repo.rebuild_unified_kg(nb.id)
    assert llm.calls == first_calls                       # no new LLM calls
    after = _cluster_descs(repo, nb.id)
    assert {d for (d, _s) in after.values()} == {d for (d, _s) in before.values()}
    assert {sig for (_d, sig) in after.values()} == {sig for (_d, sig) in before.values()}


# --- 3. invalidation --------------------------------------------------------

def test_rebuild_regenerates_only_changed_cluster(repo):
    llm = _DescLLM()
    repo.llm_client = llm
    # two independent merged concepts
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, "s1", [
        _concept_with_evidence("a1", "MOSFET", "mosfet A"),
        _concept_with_evidence("b1", "cascode", "cascode A"),
    ], [])
    repo.store_kg(nb.id, "s2", [
        _concept_with_evidence("a2", "mosfet", "mosfet B"),
        _concept_with_evidence("b2", "Cascode", "cascode B"),
    ], [])
    repo.rebuild_unified_kg(nb.id)
    assert llm.calls == 2
    baseline = _cluster_descs(repo, nb.id)

    # Change ONE cluster's evidence quotes (mutate a MOSFET member's quoted_span)
    # so ONLY the MOSFET canonical's sig changes; cascode stays byte-identical.
    with repo._write() as db:
        row = db.execute(
            "SELECT id, evidence FROM knowledge_objects "
            "WHERE notebook_id=? AND object_type='concept' "
            "AND json_extract(payload,'$.name')='MOSFET'", (nb.id,)).fetchone()
        ev = json.loads(row["evidence"])
        ev[0]["quoted_span"] = "mosfet A CHANGED"
        # Bump updated_at as any real writer (store_kg/_run_extraction) does — this
        # is what moves the rebuild's cluster-input version so the skip gate lets
        # the recompute through. A raw evidence poke that leaves updated_at stale
        # is not a real mutation in production.
        from app.services.sqlite_repository import _now as _now_ts
        db.execute("UPDATE knowledge_objects SET evidence=?, updated_at=? WHERE id=?",
                   (json.dumps(ev), _now_ts(), row["id"]))

    # force=True: this test targets the per-canonical description sub-cache, not the
    # outer skip gate; force ensures the recompute runs even if updated_at collided
    # at second resolution with the previous rebuild.
    repo.rebuild_unified_kg(nb.id, force=True)
    # exactly ONE additional description call (the MOSFET cluster only)
    assert llm.calls == 3, f"expected 1 new call, names_seen={llm.names_seen}"
    assert llm.names_seen[-1] == "MOSFET"

    after = _cluster_descs(repo, nb.id)
    # cascode canonical's (desc, sig) unchanged (reused from cache)
    def _find(descs, wanted):
        for cid, (d, sig) in descs.items():
            if d == wanted:
                return (d, sig)
        return None
    assert _find(after, "desc-for::cascode") == _find(baseline, "desc-for::cascode")


# --- 5. progress callback ---------------------------------------------------

def test_progress_callback_invoked_per_work_item(repo):
    llm = _DescLLM()
    repo.llm_client = llm
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, "s1", [
        _concept_with_evidence("a1", "MOSFET", "m1"),
        _concept_with_evidence("b1", "cascode", "c1"),
    ], [])
    repo.store_kg(nb.id, "s2", [
        _concept_with_evidence("a2", "mosfet", "m2"),
        _concept_with_evidence("b2", "Cascode", "c2"),
    ], [])

    events = []
    lock = threading.Lock()

    def progress(phase, i, n):
        with lock:
            events.append((phase, i, n))

    repo.rebuild_unified_kg(nb.id, progress=progress)

    work_n = 2   # two multi-member canonicals need descriptions
    # The progress channel now ALSO carries sub-stage banners (i==0, n==0);
    # isolate the concept_desc per-work-item progress events (n > 0).
    item_events = [(p, i, n) for (p, i, n) in events if n > 0]
    assert len(item_events) == work_n
    assert all(phase == "concept_desc" for (phase, _i, _n) in item_events)
    assert all(n == work_n for (_p, _i, n) in item_events)
    # final progress reports i == n (completed all)
    assert max(i for (_p, i, _n) in item_events) == work_n
    # i values are the 1..n sequence (order may vary due to concurrency)
    assert sorted(i for (_p, i, _n) in item_events) == list(range(1, work_n + 1))
