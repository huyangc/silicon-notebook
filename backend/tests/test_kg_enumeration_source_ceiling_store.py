"""PR-B·B1 (SQLite store): the KG enumeration page and its count honour the
per-run source ceiling.

``knowledge_object_page_rows(allowed_source_ids=...)`` keeps only objects with
an evidence item from a ceiling source, BELOW the LIMIT (no page is starved by
out-of-ceiling rows; the keyset is unchanged); ``count_knowledge`` applies the
identical predicate (``supported_by_source_ids``) plus the owner exclusion the
executor applies to private Memory rows (``excluding_owner_source_ids``), so the
count is the true denominator of the pages.  The PostgreSQL twin
(tests/postgres/test_kg_enumeration_source_ceiling_twin.py) runs the same
spec on both backends and asserts they agree.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from tests import kg_ceiling_fixture as fx


@pytest.fixture(params=[True, False], ids=["reverse_index", "authoritative_json"])
def seeded(request, tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings())
    with repo._write() as db:
        fx.seed(lambda sql, params: db.execute(sql, params), "?",
                backfilled=request.param)
    return repo


def _pages(repo, allowed, limit):
    store = repo._runtime.knowledge
    with repo._connect() as db:
        return fx.walk_pages(
            lambda after, n: store.knowledge_object_page_rows(
                db, fx.NOTEBOOK_ID, fx.OBJECT_TYPE, after, n,
                allowed_source_ids=allowed,
            ),
            limit,
        )


@pytest.mark.parametrize("ceiling_name", sorted(fx.CEILINGS))
@pytest.mark.parametrize("limit", [1, 2, 3, 50])
def test_page_walk_matches_the_reference(seeded, ceiling_name, limit):
    allowed = fx.CEILINGS[ceiling_name]
    pages = _pages(seeded, allowed, limit)
    assert [row_id for page in pages for row_id in page] == fx.reference_page_ids(allowed)
    # Pushed BELOW the LIMIT: every page but the last is full, even across the
    # ten-row out-of-ceiling stretch.
    assert all(len(page) == limit for page in pages[:-1])


def test_include_ceiling_listing_is_evidence_based(seeded):
    listed = [i for page in _pages(seeded, fx.INCLUDE_CEILING, 2) for i in page]
    # two sources one in one out → listed; owner-in/evidence-out → not; no
    # evidence → not; every evidence excluded → not; deprecated and
    # Memory-owned rows stay RAW (status/owner are the caller's filters).
    assert listed == ["ko-01", "ko-03", "ko-06", "ko-07", "ko-20", "ko-21"]
    unrestricted = [i for page in _pages(seeded, None, 50) for i in page]
    assert "ko-04" in unrestricted and "ko-09" in unrestricted


@pytest.mark.parametrize("exclusion_name", sorted(fx.EXCLUSIONS))
@pytest.mark.parametrize("ceiling_name", sorted(fx.CEILINGS))
def test_count_matches_the_reference(seeded, ceiling_name, exclusion_name):
    supported = fx.CEILINGS[ceiling_name]
    excluding = fx.EXCLUSIONS[exclusion_name]
    with seeded._connect() as db:
        count = seeded._runtime.knowledge.count_knowledge(
            db, fx.NOTEBOOK_ID, fx.OBJECT_TYPE, fx.USABLE,
            supported_by_source_ids=supported,
            excluding_owner_source_ids=excluding,
        )
    assert count == fx.reference_count(supported, excluding)


def test_count_is_the_denominator_of_the_pages(seeded):
    """What the executor keeps from the raw pages (usable status, owner not a
    Memory source) is exactly what the count counts."""
    pages = _pages(seeded, fx.INCLUDE_CEILING, 2)
    by_id = {o.id: o for o in fx.OBJECTS}
    kept = [i for page in pages for i in page
            if by_id[i].status in fx.USABLE and by_id[i].owner != "s-mem"]
    with seeded._connect() as db:
        count = seeded._runtime.knowledge.count_knowledge(
            db, fx.NOTEBOOK_ID, fx.OBJECT_TYPE, fx.USABLE,
            supported_by_source_ids=fx.INCLUDE_CEILING,
            excluding_owner_source_ids=("s-mem",),
        )
    assert count == len(kept) == 4


def test_no_ceiling_statements_are_byte_identical_and_deny_all_runs_none(seeded):
    store = seeded._runtime.knowledge
    with seeded._connect() as db:
        recorder = fx.RecordingConnection(db)
        store.knowledge_object_page_rows(recorder, fx.NOTEBOOK_ID, fx.OBJECT_TYPE, None, 5)
        store.knowledge_object_page_rows(
            recorder, fx.NOTEBOOK_ID, fx.OBJECT_TYPE, ("t", "ko-01"), 5,
            allowed_source_ids=None,
        )
        store.count_knowledge(recorder, fx.NOTEBOOK_ID, fx.OBJECT_TYPE, fx.USABLE)
        store.count_knowledge(
            recorder, fx.NOTEBOOK_ID, fx.OBJECT_TYPE, fx.USABLE,
            supported_by_source_ids=None, excluding_owner_source_ids=(),
        )
        # The exact statements and parameters from before the ceiling existed
        # (no certificate read either: nothing binds, nothing is looked up).
        count_call = (
            "SELECT COUNT(*) AS count FROM knowledge_objects "
            "WHERE notebook_id = ? AND object_type = ? AND status IN (?,?)",
            (fx.NOTEBOOK_ID, fx.OBJECT_TYPE, *fx.USABLE),
        )
        assert recorder.calls == [
            ("SELECT id, object_type, source_id, payload, evidence, status, created_at "
             "FROM knowledge_objects WHERE notebook_id = ? AND object_type = ? "
             "ORDER BY created_at, id LIMIT ?",
             (fx.NOTEBOOK_ID, fx.OBJECT_TYPE, 5)),
            ("SELECT id, object_type, source_id, payload, evidence, status, created_at "
             "FROM knowledge_objects WHERE notebook_id = ? AND object_type = ? "
             "AND (created_at, id) > (?, ?) ORDER BY created_at, id LIMIT ?",
             (fx.NOTEBOOK_ID, fx.OBJECT_TYPE, "t", "ko-01", 5)),
            count_call,
            count_call,
        ]
        deny = fx.RecordingConnection(db)
        assert store.knowledge_object_page_rows(
            deny, fx.NOTEBOOK_ID, fx.OBJECT_TYPE, None, 5, allowed_source_ids=()
        ) == []
        assert store.count_knowledge(
            deny, fx.NOTEBOOK_ID, fx.OBJECT_TYPE, fx.USABLE,
            supported_by_source_ids=(), excluding_owner_source_ids=("s-mem",),
        ) == 0
        assert deny.calls == []


def test_ceiling_binds_one_parameter_whatever_its_size(seeded):
    store = seeded._runtime.knowledge
    with seeded._connect() as db:
        recorder = fx.RecordingConnection(db)
        store.knowledge_object_page_rows(
            recorder, fx.NOTEBOOK_ID, fx.OBJECT_TYPE, None, 5,
            allowed_source_ids=fx.HUGE_CEILING,
        )
        store.count_knowledge(
            recorder, fx.NOTEBOOK_ID, fx.OBJECT_TYPE, fx.USABLE,
            supported_by_source_ids=fx.HUGE_CEILING,
            excluding_owner_source_ids=fx.HUGE_CEILING,
        )
    statements = [call for call in recorder.calls if "knowledge_objects" in call[0]
                  and "unified_kg_state" not in call[0]]
    assert [len(params) for _sql, params in statements] == [4, 6]


def test_reverse_index_probe_is_by_object_not_per_ceiling_id(seeded):
    """Query-plan pin for the certified branch: the page keeps its keyset
    index and the support probe seeks ``knowledge_object_sources`` by
    ``object_id`` ALONE (``idx_kos_object``, or the ``object_id`` prefix of
    the sync-key index — the planner may take either).  A seek on
    ``(object_id=? AND source_id=?)`` means one seek per CEILING id per
    candidate row (19.3 s vs 11 ms on a sparse 49k-id ceiling; see
    ``_object_support_sql``)."""
    store = seeded._runtime.knowledge
    with seeded._connect() as db:
        if not store.source_index_backfilled(db, fx.NOTEBOOK_ID):
            pytest.skip("authoritative branch has no reverse-index probe")
        recorder = fx.RecordingConnection(db)
        store.knowledge_object_page_rows(
            recorder, fx.NOTEBOOK_ID, fx.OBJECT_TYPE, ("t", "ko-01"), 5,
            allowed_source_ids=fx.HUGE_CEILING,
        )
        store.count_knowledge(
            recorder, fx.NOTEBOOK_ID, fx.OBJECT_TYPE, fx.USABLE,
            supported_by_source_ids=fx.HUGE_CEILING,
        )
        plans = [
            " | ".join(str(tuple(row)[3]) for row in db.execute(
                "EXPLAIN QUERY PLAN " + sql, params).fetchall())
            for sql, params in recorder.calls if "FROM knowledge_objects" in sql
        ]
    assert len(plans) == 2
    assert "idx_knowledge_objects_nb_type_created" in plans[0], plans[0]
    assert "TEMP B-TREE FOR ORDER BY" not in plans[0], plans[0]
    for plan in plans:
        assert "(object_id=?)" in plan and "SEARCH kos EXISTS USING" in plan, plan
        assert "source_id=?" not in plan, plan
        assert "SCAN kos" not in plan, plan
