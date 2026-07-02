"""P0-4: knowledge_object_sources reverse index (perf-audit docs/kg-perf-audit-16c64g.md).

_clear_source_extraction_state (delete_source / _run_extraction reparse) used to
scan EVERY knowledge_objects.evidence JSON in the notebook to find objects
referencing one source_id. These tests cover: the reverse-lookup table matching
a legacy-scan oracle on multi-source-evidence (merged) objects, backfill-on-
first-use (the scan they were going to pay anyway becomes the last one), the
second call using SQL only (no full evidence scan), forward maintenance on
store_kg / confirm_promotion / merge_knowledge, and deletion coherence.
"""
import json
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.models.schemas import MergeRequest, NotebookCreate
from app.services.sqlite_repository import SQLiteRepository, _now


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    settings = Settings()
    return SQLiteRepository(settings)


def _insert_source(repo, notebook_id: str, title: str = "Doc") -> str:
    source_id = f"src-{uuid4().hex[:10]}"
    now = _now()
    with repo._connect() as db:
        db.execute(
            """INSERT INTO sources
               (id, notebook_id, title, source_type, status, parse_status,
                file_name, file_path, file_size, file_hash, summary, doc_type,
                created_at, updated_at)
               VALUES (?, ?, ?, 'markdown', 'extracted', 'parsed',
                       'doc.md', '', 0, '', '', 'academic_paper', ?, ?)""",
            (source_id, notebook_id, title, now, now),
        )
    return source_id


def _ev(source_id: str, element_id: str = "e1") -> dict:
    return {
        "source_id": source_id, "source_title": "Doc", "element_id": element_id,
        "element_type": "paragraph", "location_label": "p1",
        "quoted_span": "span", "confidence": 1.0,
    }


def _legacy_scan_oracle(repo, notebook_id: str, source_id: str) -> set:
    """Reimplementation of the ORIGINAL full-scan behavior, used as an oracle
    to assert the reverse index returns the identical id set."""
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id, evidence FROM knowledge_objects WHERE notebook_id = ?",
            (notebook_id,),
        ).fetchall()
    out = set()
    for row in rows:
        items = json.loads(row["evidence"] or "[]")
        if any(isinstance(it, dict) and it.get("source_id") == source_id for it in items):
            out.add(row["id"])
    return out


def test_reverse_lookup_matches_legacy_scan_oracle_multi_source_evidence(repo):
    """An object whose evidence spans TWO sources (a merged object) must be
    found by a lookup on EITHER source_id — same as the legacy scan."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    s1 = _insert_source(repo, nb.id)
    s2 = _insert_source(repo, nb.id)
    s3 = _insert_source(repo, nb.id)

    objects = [
        {"local_id": "A", "object_type": "concept", "payload": {"name": "Merged"},
         "evidence": [_ev(s1), _ev(s2)]},          # spans two sources
        {"local_id": "B", "object_type": "concept", "payload": {"name": "OnlyS3"},
         "evidence": [_ev(s3)]},
        {"local_id": "C", "object_type": "concept", "payload": {"name": "NoEvidence"},
         "evidence": []},
    ]
    repo.store_kg(nb.id, None, objects, [])

    for target in (s1, s2, s3):
        oracle = _legacy_scan_oracle(repo, nb.id, target)
        with repo._connect() as db:
            found = set(repo._find_stale_knowledge_ids_for_source(db, target, nb.id))
        assert found == oracle
        assert found  # sanity: each source actually matches something


def test_first_use_backfill_populates_and_marks(repo):
    """A pre-existing (pre-migration-style) notebook with knowledge_objects rows
    but an EMPTY knowledge_object_sources table (as if it predates this feature)
    gets populated + marked backfilled on the first lookup."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    s1 = _insert_source(repo, nb.id)
    objects = [
        {"local_id": "A", "object_type": "concept", "payload": {"name": "X"},
         "evidence": [_ev(s1)]},
    ]
    repo.store_kg(nb.id, None, objects, [])

    # Simulate a pre-existing DB: wipe the reverse index + backfilled marker as
    # if this notebook's data predates the feature (forward maintenance already
    # populated it via store_kg above — undo that to test the legacy path).
    with repo._write() as db:
        db.execute("DELETE FROM knowledge_object_sources WHERE notebook_id=?", (nb.id,))
        db.execute(
            "UPDATE unified_kg_state SET source_index_backfilled=0 WHERE notebook_id=?",
            (nb.id,),
        )

    with repo._connect() as db:
        assert not repo._source_index_backfilled(db, nb.id)
        with repo._connect() as db2:
            count_before = db2.execute(
                "SELECT COUNT(*) c FROM knowledge_object_sources WHERE notebook_id=?", (nb.id,)
            ).fetchone()["c"]
    assert count_before == 0

    with repo._write() as db:
        found = repo._find_stale_knowledge_ids_for_source(db, s1, nb.id)
    assert len(found) == 1

    with repo._connect() as db:
        assert repo._source_index_backfilled(db, nb.id)
        rows = db.execute(
            "SELECT object_id, source_id FROM knowledge_object_sources WHERE notebook_id=?",
            (nb.id,),
        ).fetchall()
    assert {(r["object_id"], r["source_id"]) for r in rows} == {(found[0], s1)}


def test_second_lookup_uses_sql_only_no_full_evidence_scan(repo, monkeypatch):
    """After the first (backfilling) call, a second call for the SAME notebook
    must not re-scan every knowledge_objects row — it hits the indexed SQL path.

    sqlite3.Connection is a C-level immutable type (can't monkeypatch .execute
    on it directly), so we spy one level up: _source_ids_from_evidence is the
    ONLY place the legacy scan parses an evidence column, called once per row
    it iterates. A backfilled lookup must call it zero times."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    s1 = _insert_source(repo, nb.id)
    objects = [
        {"local_id": "A", "object_type": "concept", "payload": {"name": "X"},
         "evidence": [_ev(s1)]},
    ]
    repo.store_kg(nb.id, None, objects, [])

    with repo._write() as db:
        first = set(repo._find_stale_knowledge_ids_for_source(db, s1, nb.id))
        assert repo._source_index_backfilled(db, nb.id)

    calls = {"n": 0}
    orig = SQLiteRepository._source_ids_from_evidence

    def spy(evidence_json):
        calls["n"] += 1
        return orig(evidence_json)

    monkeypatch.setattr(SQLiteRepository, "_source_ids_from_evidence", staticmethod(spy))
    try:
        with repo._write() as db:
            second = set(repo._find_stale_knowledge_ids_for_source(db, s1, nb.id))
    finally:
        pass

    assert second == first
    assert calls["n"] == 0, "backfilled notebook must not re-parse any evidence column"


def test_store_kg_forward_maintenance_populates_reverse_index(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    s1 = _insert_source(repo, nb.id)
    s2 = _insert_source(repo, nb.id)
    objects = [
        {"local_id": "A", "object_type": "concept", "payload": {"name": "X"},
         "evidence": [_ev(s1), _ev(s2)]},
    ]
    repo.store_kg(nb.id, None, objects, [])
    with repo._connect() as db:
        rows = db.execute(
            "SELECT source_id FROM knowledge_object_sources WHERE notebook_id=?",
            (nb.id,),
        ).fetchall()
    assert {r["source_id"] for r in rows} == {s1, s2}


def test_merge_knowledge_updates_reverse_index_for_target(repo):
    """merge_knowledge folds source's evidence into into_id: into_id's reverse-
    index rows must gain any new source_ids; source_id's own rows (it is only
    deprecated, not deleted) remain untouched."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    s1 = _insert_source(repo, nb.id)
    s2 = _insert_source(repo, nb.id)
    objects = [
        {"local_id": "A", "object_type": "claim", "payload": {"name": "A"},
         "evidence": [_ev(s1)]},
        {"local_id": "B", "object_type": "claim", "payload": {"name": "B"},
         "evidence": [_ev(s2, element_id="e2")]},
    ]
    repo.store_kg(nb.id, None, objects, [])
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id, object_type FROM knowledge_objects WHERE notebook_id=? ORDER BY payload",
            (nb.id,),
        ).fetchall()
    by_name = {}
    with repo._connect() as db:
        for r in db.execute(
            "SELECT id, payload FROM knowledge_objects WHERE notebook_id=?", (nb.id,)
        ).fetchall():
            by_name[json.loads(r["payload"])["name"]] = r["id"]
    a_id, b_id = by_name["A"], by_name["B"]

    repo.merge_knowledge(nb.id, a_id, MergeRequest(into_id=b_id))

    with repo._connect() as db:
        into_sources = {
            r["source_id"] for r in db.execute(
                "SELECT source_id FROM knowledge_object_sources WHERE object_id=?", (b_id,)
            ).fetchall()
        }
        src_sources = {
            r["source_id"] for r in db.execute(
                "SELECT source_id FROM knowledge_object_sources WHERE object_id=?", (a_id,)
            ).fetchall()
        }
    assert into_sources == {s1, s2}   # gained s1 from the merged-away object
    assert src_sources == {s1}        # unchanged (deprecated, not deleted)


def test_delete_source_cleans_join_rows(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    s1 = _insert_source(repo, nb.id)
    objects = [
        {"local_id": "A", "object_type": "concept", "payload": {"name": "X"},
         "evidence": [_ev(s1)]},
    ]
    repo.store_kg(nb.id, s1, objects, [])
    with repo._connect() as db:
        before = db.execute(
            "SELECT COUNT(*) c FROM knowledge_object_sources WHERE source_id=?", (s1,)
        ).fetchone()["c"]
    assert before == 1

    repo.delete_source(s1)

    with repo._connect() as db:
        after = db.execute(
            "SELECT COUNT(*) c FROM knowledge_object_sources WHERE source_id=?", (s1,)
        ).fetchone()["c"]
    assert after == 0


def test_reparse_source_cleans_and_does_not_leak_join_rows(repo):
    """_run_extraction (reparse) clears BOTH evidence-referenced objects and
    directly-authored objects (source_id column) — both deletion sites must
    clean knowledge_object_sources."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    s1 = _insert_source(repo, nb.id)
    objects = [
        {"local_id": "A", "object_type": "concept", "payload": {"name": "X"},
         "evidence": [_ev(s1)]},
    ]
    repo.store_kg(nb.id, s1, objects, [])

    with repo._write() as db:
        repo._clear_source_extraction_state(db, s1, nb.id, clear_embeddings=False)

    with repo._connect() as db:
        remaining_objs = db.execute(
            "SELECT COUNT(*) c FROM knowledge_objects WHERE notebook_id=?", (nb.id,)
        ).fetchone()["c"]
        remaining_join = db.execute(
            "SELECT COUNT(*) c FROM knowledge_object_sources WHERE notebook_id=?", (nb.id,)
        ).fetchone()["c"]
    assert remaining_objs == 0
    assert remaining_join == 0


def test_copy_notebook_self_heals_reverse_index_for_the_copy(repo):
    """copy_notebook does not copy unified_kg_state/knowledge_object_sources
    (derived indexes are intentionally excluded from the deep copy — see the
    function's docstring). The new notebook's unified_kg_state row is therefore
    absent (source_index_backfilled defaults to unbackfilled), so the FIRST
    source-clear call on the copy correctly takes the legacy-scan path and
    self-heals its own reverse index — no explicit wiring needed in
    copy_notebook itself."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    s1 = _insert_source(repo, nb.id)
    objects = [
        {"local_id": "A", "object_type": "concept", "payload": {"name": "X"},
         "evidence": [_ev(s1)]},
    ]
    repo.store_kg(nb.id, s1, objects, [])

    copy = repo.copy_notebook(nb.id, new_owner_id="user-local")

    with repo._connect() as db:
        assert not repo._source_index_backfilled(db, copy.id)
        copied_source = db.execute(
            "SELECT id FROM sources WHERE notebook_id=?", (copy.id,)
        ).fetchone()["id"]

    with repo._write() as db:
        found = repo._find_stale_knowledge_ids_for_source(db, copied_source, copy.id)
    assert len(found) == 1
    with repo._connect() as db:
        assert repo._source_index_backfilled(db, copy.id)
