"""Sub-stage instrumentation of rebuild_unified_kg.

rebuild_unified_kg emits a banner per sub-stage on TWO channels:
  1. the event_log logger ("silicon_notebook.events") at INFO, prefixed
     "kg-rebuild[<nb>] ".
  2. the progress callback as a banner: progress(msg, 0, 0)  (i==0, n==0).

The instrumentation is pure logging: it must NOT change the returned cluster
count, the persisted concept_clusters, or the progress=None path (aside from
the always-emitted INFO lines).
"""
import json

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("KG_CONCEPT_DESC_ENABLED", "false")  # desc disabled path
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _concept(local_id, name, span=""):
    return {"local_id": local_id, "object_type": "concept",
            "payload": {"name": name, "section_path": "1"},
            "evidence": ([{"source_id": "s", "source_title": "D", "element_id": "e",
                           "element_type": "p", "location_label": "1",
                           "quoted_span": span, "confidence": 1.0}] if span else [])}


def _seed_multimember_with_claim(repo):
    """One multi-member concept canonical (MOSFET across 2 sources) + one claim."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, "s1", [
        _concept("a", "MOSFET", "mosfet is a transistor"),
        {"local_id": "c1", "object_type": "claim",
         "payload": {"name": "gain is high", "section_path": ""}, "evidence": []},
    ], [])
    repo.store_kg(nb.id, "s2", [_concept("b", "mosfet", "the MOSFET conducts")], [])
    return nb


def _clusters_snapshot(repo, nb_id):
    with repo._connect() as db:
        rows = db.execute(
            "SELECT canonical_id, member_object_id, canonical_name, object_type "
            "FROM concept_clusters WHERE notebook_id=? "
            "ORDER BY object_type, canonical_id, member_object_id",
            (nb_id,)).fetchall()
    return [tuple(r) for r in rows]


def test_all_progress_calls_are_banners(repo):
    nb = _seed_multimember_with_claim(repo)
    events = []
    repo.rebuild_unified_kg(nb.id, progress=lambda msg, i, n: events.append((msg, i, n)))
    assert events, "expected at least one instrumentation banner"
    # With desc disabled there is no concept_desc i/n progress; every call is a banner.
    assert all(n == 0 and i == 0 for (_msg, i, n) in events)


def test_banner_sequence_contains_key_stages(repo):
    nb = _seed_multimember_with_claim(repo)
    msgs = []
    repo.rebuild_unified_kg(nb.id, progress=lambda msg, i, n: msgs.append(msg))
    joined = "\n".join(msgs)
    assert "concept: streamed" in joined
    assert "concept: clustered" in joined
    assert "concept: wrote clusters" in joined
    assert "concept: descriptions skipped" in joined      # desc disabled
    assert "claim: streamed+clustered+wrote" in joined
    assert "pending refresh" in joined
    # final DONE banner with a total time
    assert any(m.startswith("DONE ") and "total" in m for m in msgs), msgs


def test_info_lines_emitted(repo, caplog):
    nb = _seed_multimember_with_claim(repo)
    with caplog.at_level("INFO", logger="silicon_notebook.events"):
        repo.rebuild_unified_kg(nb.id)
    lines = [r.getMessage() for r in caplog.records
             if "kg-rebuild[" in r.getMessage()]
    assert lines, "expected kg-rebuild[...] INFO lines"
    assert any("concept: streamed" in m for m in lines)
    assert any("DONE" in m and "total" in m for m in lines)


def test_instrumentation_is_side_effect_free_on_data(repo):
    # Rebuild the SAME notebook twice: once with progress=None, once with a
    # capturing callback. member_object_id / canonical_name / object_type are
    # stable across rebuilds of a fixed notebook (only the concept_clusters row
    # `id` suffix is random), so they form the correct side-effect invariant.
    nb = _seed_multimember_with_claim(repo)

    count_none = repo.rebuild_unified_kg(nb.id, progress=None)
    snap_none = _clusters_snapshot(repo, nb.id)

    events = []
    count_cap = repo.rebuild_unified_kg(nb.id, progress=lambda m, i, n: events.append((m, i, n)))
    snap_cap = _clusters_snapshot(repo, nb.id)

    # cluster counts identical
    assert count_none == count_cap
    # written concept_clusters identical modulo the per-write random cid suffix.
    def _stable(snap):
        return sorted((otype, cname, mid) for (_cid, mid, cname, otype) in snap)
    assert _stable(snap_none) == _stable(snap_cap)
    assert events  # the capture run did emit banners
