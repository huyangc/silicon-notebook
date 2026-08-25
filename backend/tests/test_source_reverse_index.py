"""P0-4: knowledge_object_sources reverse index (perf-audit docs/kg-perf-audit-16c64g.md).

_clear_source_extraction_state (delete_source / _run_extraction reparse) used to
read, parse and backfill EVERY knowledge_objects.evidence row in the notebook
while serving the request. These tests cover: the indexed reverse lookup,
database-side legacy filtering without an interactive backfill, bounded bulk
deletion, forward maintenance, and deletion coherence.
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


def test_new_notebook_starts_with_complete_empty_source_index(repo):
    nb = repo.create_notebook(NotebookCreate(name="new"))
    with repo._connect() as db:
        assert repo._source_index_backfilled(db, nb.id)

    source_id = _insert_source(repo, nb.id)
    repo.store_kg(nb.id, source_id, [{
        "local_id": "A",
        "object_type": "concept",
        "payload": {"name": "Cosmos 3"},
        "evidence": [_ev(source_id)],
    }], [])
    with repo._connect() as db:
        assert repo._source_index_backfilled(db, nb.id)
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_object_sources "
            "WHERE notebook_id=?",
            (nb.id,),
        ).fetchone()["c"] == 1


def test_smoke_seed_maintains_certified_source_index(repo):
    nb = repo.create_notebook(NotebookCreate(name="seed"))
    source_id = _insert_source(repo, nb.id)
    object_id = repo.maintenance.seed_rule_object(
        nb.id,
        payload={"name": "seeded rule"},
        evidence=[_ev(source_id)],
        source_id=source_id,
    )
    with repo._connect() as db:
        assert repo._source_index_backfilled(db, nb.id)
        row = db.execute(
            "SELECT source_id FROM knowledge_object_sources "
            "WHERE notebook_id=? AND object_id=?",
            (nb.id, object_id),
        ).fetchone()
    assert row["source_id"] == source_id


def test_graph_clear_preserves_unknown_source_index_state(repo):
    """Clearing user KG cannot certify surviving historical projections."""
    nb = repo.create_notebook(NotebookCreate(name="legacy"))
    with repo._write() as db:
        db.execute(
            "UPDATE unified_kg_state SET source_index_backfilled=0 "
            "WHERE notebook_id=?",
            (nb.id,),
        )

    repo.delete_notebook_kg(nb.id)

    with repo._connect() as db:
        assert not repo._source_index_backfilled(db, nb.id)


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


def test_unbackfilled_lookup_filters_in_database_without_backfilling(repo):
    """Interactive legacy lookup finds the right object but leaves the explicit
    batch-ingest backfill and its completion marker untouched."""
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
        assert not repo._source_index_backfilled(db, nb.id)
        rows = db.execute(
            "SELECT object_id, source_id FROM knowledge_object_sources WHERE notebook_id=?",
            (nb.id,),
        ).fetchall()
    assert rows == []


def test_unbackfilled_lookup_only_expands_valid_top_level_arrays(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    s1 = _insert_source(repo, nb.id)
    now = _now()
    with repo._write() as db:
        db.executemany(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
            "VALUES (?,?, 'concept','approved','','{}',?,'',?,?)",
            [
                ("bad-json", nb.id, "not-json", now, now),
                ("mixed-json", nb.id, json.dumps(["plain", 3, _ev(s1)]), now, now),
                (
                    "top-object",
                    nb.id,
                    json.dumps({"wrapper": {"source_id": s1}}),
                    now,
                    now,
                ),
                ("top-string", nb.id, json.dumps(s1), now, now),
                ("top-number", nb.id, "3", now, now),
                ("top-null", nb.id, "null", now, now),
                ],
            )
        db.execute(
            "UPDATE unified_kg_state SET source_index_backfilled=0 WHERE notebook_id=?",
            (nb.id,),
        )
    with repo._write() as db:
        found = repo._find_stale_knowledge_ids_for_source(db, s1, nb.id)
    assert found == ["mixed-json"]


def test_clear_source_batches_large_object_id_sets(repo):
    """Stale and direct-only matches stay below the 500-id batch rail."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    s1 = _insert_source(repo, nb.id)
    now = _now()
    stale_rows = [
        (
            f"obj-{idx:04d}", nb.id, json.dumps(_ev(s1)), s1, now, now,
        )
        for idx in range(1100)
    ]
    direct_rows = [
        (f"direct-{idx:04d}", nb.id, s1, now, now)
        for idx in range(1100)
    ]
    delete_statements: list[str] = []
    with repo._write() as db:
        db.executemany(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
            "VALUES (?,?,'concept','approved','','{}','[' || ? || ']',?,?,?)",
            stale_rows,
        )
        db.executemany(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
            "VALUES (?,?,'concept','approved','','{}','[]',?,?,?)",
            direct_rows,
        )
        db.executemany(
            "INSERT INTO knowledge_object_sources (object_id,source_id,notebook_id) "
            "VALUES (?,?,?)",
            [(row[0], s1, nb.id) for row in stale_rows],
        )
        repo._mark_source_index_backfilled(db, nb.id)
        db.set_trace_callback(delete_statements.append)
        repo._clear_source_extraction_state(db, s1, nb.id, clear_embeddings=False)
        db.set_trace_callback(None)

    bounded_deletes = [
        statement
        for statement in delete_statements
        if statement.startswith("DELETE FROM knowledge_objects WHERE id IN (")
        or statement.startswith(
            "DELETE FROM knowledge_embeddings WHERE object_id IN ("
        )
        or statement.startswith(
            "DELETE FROM knowledge_object_sources WHERE object_id IN ("
        )
    ]
    assert len(bounded_deletes) >= 9
    assert all(
        statement.count("'obj-") + statement.count("'direct-") <= 500
        for statement in bounded_deletes
    )
    assert any(statement.count("'obj-") == 500 for statement in bounded_deletes)
    assert any(statement.count("'direct-") == 500 for statement in bounded_deletes)

    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=?", (nb.id,)
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_object_sources WHERE notebook_id=?",
            (nb.id,),
        ).fetchone()["c"] == 0


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


def test_unbackfilled_clear_advances_the_legacy_scan_cursor(repo):
    """Legacy batches must not rescan the same notebook prefix each round."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    s1 = _insert_source(repo, nb.id)
    now = _now()
    statements: list[str] = []
    with repo._write() as db:
        db.executemany(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
            "VALUES (?,?,'concept','approved','','{}',?,'',?,?)",
            [
                (f"legacy-{idx:04d}", nb.id, json.dumps([_ev(s1)]), now, now)
                for idx in range(1100)
                ],
            )
        db.execute(
            "UPDATE unified_kg_state SET source_index_backfilled=0 WHERE notebook_id=?",
            (nb.id,),
        )
        db.set_trace_callback(statements.append)
        repo._clear_source_extraction_state(db, s1, nb.id, clear_embeddings=False)
        db.set_trace_callback(None)

    legacy_selects = [
        statement for statement in statements
        if statement.startswith("SELECT DISTINCT ko.id AS object_id")
    ]
    assert len(legacy_selects) == 4
    assert "ko.id > ''" in legacy_selects[0]
    assert "ko.id > 'legacy-0499'" in legacy_selects[1]
    assert "ko.id > 'legacy-0999'" in legacy_selects[2]


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


def test_copy_notebook_uses_legacy_filter_without_mutating_reverse_index(repo):
    """copy_notebook does not copy unified_kg_state/knowledge_object_sources
    (derived indexes are intentionally excluded from the deep copy — see the
    function's docstring). The new notebook's unified_kg_state row is therefore
    absent (source_index_backfilled defaults to unbackfilled), so the FIRST
    source-clear lookup on the copy correctly takes the database JSON filter
    path without performing the explicit batch backfill."""
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
        assert not repo._source_index_backfilled(db, copy.id)
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_object_sources WHERE notebook_id=?",
            (copy.id,),
        ).fetchone()["c"] == 0
