"""Shared semantic assertions for SQLite and PostgreSQL concept ranking."""
from __future__ import annotations

import json

import pytest

from app.domain.concept_ranking_contracts import ConceptRankWrite, PrecomputedRankRow
from app.models.schemas import NotebookCreate
from tests.kg_findings_store_cases import T0, _writer, payloads


def seed(repo, *, postgres):
    nb = repo.create_notebook(NotebookCreate(name="rank")).id
    with repo._runtime.database.write() as db:
        ins = _writer(db, postgres)
        for sid, kind in [
            *((f"s{i:02}", "markdown") for i in range(12)),
            ("memory", "memory"), ("knowhow", "knowhow"),
        ]:
            ins("sources", "id,notebook_id,title,source_type,status,created_at,updated_at",
                (sid, nb, sid, kind, "extracted", T0, T0))
        objects = [
            *((f"wide-{i:02}", f"s{i:02}", "广义概念", "approved", "wide")
              for i in range(9)),
            ("wide-09", "s00", "广义概念复述", "approved", "wide"),
            ("blank-0", "s00", "", "approved", "blank"),
            ("blank-1", "s01", "有效名称", "approved", "blank"),
            ("space-0", "s00", "  ", "approved", "space"),
            ("space-1", "s01", "有空格前缀", "reviewed", "space"),
            ("scope-0", "s00", "范围外", "approved", "scope"),
            ("scope-1", "s01", "范围内", "approved", "scope"),
            ("same-a", "s01", "同名", "approved", None),
            ("same-b", "s02", "同名", "approved", None),
            ("single", "s03", "单例", "approved", None),
            ("stale", "s04", "未发布簇", "approved", None),
            ("dead", "s05", "过期", "deprecated", "wide"),
            ("memory-object", "memory", "同名", "approved", "wide"),
            ("knowhow-object", "knowhow", "同名", "approved", "wide"),
            ("orphan", "gone", "孤儿", "approved", "wide"),
            ("empty-owner", "", "无 owner", "approved", "wide"),
        ]
        for oid, sid, name, status, canonical in objects:
            ins("knowledge_objects",
                "id,notebook_id,object_type,status,payload,evidence,source_id,created_at,updated_at",
                (oid, nb, "concept", status, json.dumps({"name": name}), "[]", sid, T0, T0),
                json_columns=("payload", "evidence"))
            if canonical:
                ins("concept_clusters",
                    "id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,created_at,generation",
                    ("cc-" + oid, nb, canonical, oid, name, "concept", T0, 0))
        ins("concept_clusters",
            "id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,created_at,generation",
            ("cc-stale", nb, "unpublished", "stale", "未发布簇", "concept", T0, 1))
        ins("knowledge_objects",
            "id,notebook_id,object_type,status,payload,evidence,source_id,created_at,updated_at",
            ("claim", nb, "claim", "approved", "{}", "[]", "s00", T0, T0),
            json_columns=("payload", "evidence"))
    return nb


EXPECTED = {
    "wide": (9, 10), "blank": (2, 2), "space": (2, 2), "scope": (2, 2),
    "same-a": (1, 1), "same-b": (1, 1), "single": (1, 1), "stale": (1, 1),
}


def check_counts(repo, *, postgres):
    nb = seed(repo, postgres=postgres)
    store = repo._runtime.unified_kg
    with store.read_snapshot() as db:
        assert store.concept_rank_visible_source_ids(db, nb) == [f"s{i:02}" for i in range(12)]
        assert {c: (s, o) for c, s, o in store.concept_rank_full_rows(
            db, nb, ceiling=None)} == EXPECTED
        for ceiling in (None, frozenset({"s01"}), frozenset()):
            full = store.concept_rank_full_rows(db, nb, ceiling=ceiling)
            recount = store.concept_recount_rows(db, nb, list(EXPECTED), ceiling=ceiling)
            assert sorted(recount) == sorted(full)
        assert dict((c, (s, o)) for c, s, o in store.concept_rank_full_rows(
            db, nb, ceiling=frozenset({"s01"}))) == {
                "wide": (1, 1), "blank": (1, 1), "space": (1, 1),
                "scope": (1, 1), "same-a": (1, 1)}
        assert store.concept_rank_candidate_canonicals(
            db, nb, ["wide-00", "single", "stale", "claim", "missing"]) == {
                "wide-00": "wide", "single": "single", "stale": "stale"}
        assert store.concept_rank_member_counts(
            db, nb, ["wide", "blank", "single", "unpublished"]) == {"wide": 15, "blank": 2}
        assert store.concept_rank_representatives(db, nb, list(EXPECTED)) == {
            "wide": "wide-00", "blank": "blank-0", "space": "space-0", "scope": "scope-0",
            "same-a": "same-a", "same-b": "same-b", "single": "single", "stale": "stale"}


def check_names_citations(repo, *, postgres):
    nb = seed(repo, postgres=postgres)
    store = repo._runtime.unified_kg
    with store.read_snapshot() as db:
        # The first five ids are dead, empty-owner, knowhow-object,
        # memory-object and orphan: scan bounds the raw ordered member window.
        assert store.concept_rank_names(
            db, nb, ["wide"], ceiling=None, scan=5) == {}
        assert store.concept_rank_names(
            db, nb, ["wide"], ceiling=None, scan=6) == {"wide": "广义概念"}
        assert store.concept_rank_citation_rows(
            db, nb, ["wide"], ceiling=None, scan=5) == []
        assert [(c, oid, sid) for c, oid, sid, _ in store.concept_rank_citation_rows(
            db, nb, ["wide"], ceiling=None, scan=6)] == [("wide", "wide-00", "s00")]
        for scan, expected in ((1, {}), (2, {"blank": "有效名称", "space": "有空格前缀"})):
            assert store.concept_rank_names(
                db, nb, ["blank", "space"], ceiling=None, scan=scan) == expected
        assert store.concept_rank_names(
            db, nb, ["scope"], ceiling=frozenset({"s01"}), scan=1) == {}
        assert store.concept_rank_names(
            db, nb, ["scope"], ceiling=frozenset({"s01"}), scan=2) == {"scope": "范围内"}
        assert store.concept_rank_names(
            db, nb, ["same-a", "same-b", "single"], ceiling=None, scan=2) == {
                "same-a": "同名", "same-b": "同名", "single": "单例"}
        rows = store.concept_rank_citation_rows(
            db, nb, ["scope", "blank", "single"], ceiling=None, scan=2)
        assert [(c, oid, sid) for c, oid, sid, _ in rows] == [
            ("scope", "scope-0", "s00"), ("scope", "scope-1", "s01"),
            ("blank", "blank-0", "s00"), ("blank", "blank-1", "s01"),
            ("single", "single", "s03")]
        assert all(sum(c == can for c, *_ in rows) <= 2 for can in ("scope", "blank", "single"))
        assert store.concept_rank_citation_rows(
            db, nb, ["scope"], ceiling=frozenset({"s01"}), scan=1) == []
        assert store.concept_rank_names(db, nb, ["scope"], ceiling=frozenset(), scan=2) == {}


def publish(repo, nb, ranks):
    with repo._runtime.database.write() as db:
        repo._runtime.unified_kg.replace_kg_analysis_artifacts(
            db, nb, 7, [], [], payloads(), T0, findings_rows=(), prune_marks=(),
            concept_ranks=ranks)


def check_publication(repo, *, postgres):
    nb = seed(repo, postgres=postgres)
    store = repo._runtime.unified_kg
    rank = ConceptRankWrite(7, 3, (
        PrecomputedRankRow(1, "wide", "wide-00", 9, 9),
        PrecomputedRankRow(2, "single", "single", 1, 1)))
    with store.read_snapshot() as db:
        assert store.concept_rank_header(db, nb) is None
    publish(repo, nb, rank)
    with store.read_snapshot() as db:
        assert store.concept_rank_header(db, nb) == (7, 3)
        rows = store.concept_rank_precomputed_rows(db, nb)
        assert [r["current_canonical_id"] for r in rows] == ["wide", "single"]
        assert [r["rank"] for r in rows] == [1, 2]
        assert rows[0] == dict(rank=1, canonical_id="wide", representative_object_id="wide-00",
                              source_count=9, object_count=9, current_canonical_id="wide")
    publish(repo, nb, None)
    with store.read_snapshot() as db:
        assert store.concept_rank_precomputed_rows(db, nb) == rows
    p = "%s" if postgres else "?"
    with repo._runtime.database.write() as db:
        db.execute(f"UPDATE concept_clusters SET canonical_id='merged' WHERE notebook_id={p} "
                   "AND member_object_id='wide-00'", (nb,))
        db.execute(f"DELETE FROM knowledge_objects WHERE notebook_id={p} AND id='single'", (nb,))
    with store.read_snapshot() as db:
        assert [r["current_canonical_id"] for r in store.concept_rank_precomputed_rows(
            db, nb)] == ["merged", None]
    for invalid in (
        ConceptRankWrite(7, 3, (PrecomputedRankRow(2, "wide", "wide-00", 9, 9),)),
        ConceptRankWrite(7, 3, (PrecomputedRankRow(1, "wide", "wide-00", -1, 9),)),
        ConceptRankWrite(7, 3, tuple(PrecomputedRankRow(i, str(i), "", 1, 1) for i in range(1, 82))),
    ):
        with pytest.raises(ValueError):
            publish(repo, nb, invalid)
    with store.read_snapshot() as db:
        assert len(store.concept_rank_precomputed_rows(db, nb)) == 2
    publish(repo, nb, ConceptRankWrite(8, 4, (PrecomputedRankRow(1, "blank", "blank-0", 2, 2),)))
    with store.read_snapshot() as db:
        assert store.concept_rank_header(db, nb) == (8, 4)
        assert [r["canonical_id"] for r in store.concept_rank_precomputed_rows(db, nb)] == ["blank"]
    publish(repo, nb, ConceptRankWrite(9, 5, ()))
    with store.read_snapshot() as db:
        assert store.concept_rank_header(db, nb) == (9, 5)
        assert store.concept_rank_precomputed_rows(db, nb) == []


def check_deletion(repo, *, postgres):
    nb = seed(repo, postgres=postgres)
    store = repo._runtime.unified_kg
    ranks = ConceptRankWrite(7, 3, (PrecomputedRankRow(1, "wide", "wide-00", 9, 9),))
    publish(repo, nb, ranks)
    with repo._runtime.database.write() as db:
        repo._runtime.knowledge.delete_notebook_graph_rows(db, nb, T0)
    with store.read_snapshot() as db:
        assert store.concept_rank_header(db, nb) is None
    publish(repo, nb, ranks)
    repo.delete_notebook(nb)
    with store.read_snapshot() as db:
        assert store.concept_rank_header(db, nb) is None
