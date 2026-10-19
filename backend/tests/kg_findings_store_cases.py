"""Shared world and expectations for the KG analysis findings store primitives
(SQLite v97 / PostgreSQL 0077; design
docs/superpowers/specs/2026-10-19-kg-analysis-findings-design_zh.md §8).

``tests/test_kg_findings_store.py`` runs every case on SQLite and
``tests/postgres/test_kg_findings_store_pg.py`` on PostgreSQL; both compare the
observed value with the SAME literal below, so the two backends are held to
byte-identical results, not to "something plausible each". Every case goes
through ``repo._runtime.<store>.<method>``.
"""
from __future__ import annotations

import json
import math

from app.domain.kg_analysis_contracts import (
    ARTIFACT_COMMUNITY_EDGES,
    ARTIFACT_FINDINGS,
    ARTIFACT_KINDS,
    stamp_ledger_contract,
)
from app.domain.kg_findings_contracts import ALL_RULES, RULES_VERSION
from app.models.schemas import NotebookCreate
from app.services.kg_analysis_precompute import stamp_cluster_seq

# Timestamps carry microseconds and an explicit UTC offset: the shape SQLite
# stores verbatim and PostgreSQL's timestamptz reads back through
# ``datetime.isoformat()``, so both backends return the same text.
T0 = "2026-10-19T08:00:00.123456+00:00"
T1 = "2026-10-19T09:00:00.111111+00:00"
T2 = "2026-10-19T10:00:00.222222+00:00"


class World:
    def __init__(self, notebook_id: str, other_id: str) -> None:
        self.nb = notebook_id
        self.other = other_id


def _writer(db, postgres: bool):
    p = "%s" if postgres else "?"
    js = "%s::jsonb" if postgres else "?"

    def ins(table: str, columns: str, values: tuple, json_columns=()) -> None:
        names = [c.strip() for c in columns.split(",")]
        marks = ",".join(js if n in json_columns else p for n in names)
        db.execute(f"INSERT INTO {table}({columns}) VALUES ({marks})", values)

    return ins


def seed(repo, *, postgres: bool) -> World:
    """Two notebooks; in the first, two document sources (one mid-parse), a
    knowhow and a Memory source (hidden synthetic sources), concept objects
    clustered under one canonical in the published generation (plus a deprecated
    member, a hidden-source member, an orphan-source member and a member whose
    only cluster row is in an unpublished generation), one unclustered object,
    extraction runs with a created_at tie, and merge candidates in every status."""
    world = World(
        repo.create_notebook(NotebookCreate(name="findings")).id,
        repo.create_notebook(NotebookCreate(name="findings-other")).id,
    )
    nb, other = world.nb, world.other
    with repo._runtime.database.write() as db:
        ins = _writer(db, postgres)
        for sid, notebook, title, stype, status, parse, chunked in (
            ("src-a", nb, "Paper A", "markdown", "extracted", "parsed", T0),
            ("src-b", nb, "Paper B", "markdown", "parsing", "parsing", None),
            ("src-k", nb, "Secret table", "knowhow", "extracted", "parsed", T0),
            ("src-m", nb, "My memory", "memory", "extracted", "parsed", T0),
            ("src-o", other, "Elsewhere", "markdown", "extracted", "parsed", T0),
        ):
            ins("sources", "id,notebook_id,title,source_type,status,parse_status,"
                "chunked_at,created_at,updated_at",
                # src-b imported later: ``created_at`` is F2's tie-break and must
                # come back from its own column, not from another timestamp.
                (sid, notebook, title, stype, status, parse, chunked,
                 T1 if sid == "src-b" else T0, T0))
        for rid, sid, status, created in (
            ("er-a1", "src-a", "completed", T1),
            ("er-a2", "src-a", "completed", T2),
            ("er-a3", "src-a", "failed", T2),
            ("er-b1", "src-b", "running", T1),
        ):
            ins("extraction_runs", "id,notebook_id,source_id,run_type,status,"
                "error_message,created_at,updated_at",
                (rid, nb, sid, "kg", status, "", created, created))
        for oid, sid, name, status in (
            ("ko-a1", "src-a", "RMSNorm", "approved"),
            ("ko-a2", "src-a", "RMS Norm", "reviewed"),
            ("ko-a3", "src-a", "RMSNorm", "approved"),
            ("ko-a4", "src-a", "Stale gen", "approved"),
            ("ko-b1", "src-b", "Layer Norm", "approved"),
            ("ko-b2", "src-b", "Dead", "deprecated"),
            ("ko-k1", "src-k", "RMSNorm (knowhow)", "approved"),
            ("ko-m1", "src-m", "RMSNorm (memory)", "approved"),
            ("ko-x1", "src-gone", "Orphan", "approved"),
        ):
            ins("knowledge_objects", "id,notebook_id,object_type,status,payload,"
                "evidence,source_id,created_at,updated_at",
                (oid, nb, "concept", status, json.dumps({"name": name}), "[]",
                 sid, T0, T0),
                json_columns=("payload", "evidence"))
        for member, generation in (
            ("ko-a1", 0), ("ko-a2", 0), ("ko-a3", 0), ("ko-b2", 0),
            ("ko-k1", 0), ("ko-m1", 0), ("ko-x1", 0), ("ko-a4", 1),
        ):
            ins("concept_clusters", "id,notebook_id,canonical_id,member_object_id,"
                "canonical_name,object_type,created_at,generation",
                (f"cc-{member}-{generation}", nb, "K-rmsnorm", member,
                 "RMSNorm (knowhow)", "concept", T0, generation))
        for cid, notebook, a, b, status, seed_a, seed_b in (
            ("mc-1", nb, "K-rmsnorm", "K-rms norm", "confirmed", "rmsnorm", "rms norm"),
            ("mc-2", nb, "K-layer", "K-rmsnorm", "rejected", "layer", ""),
            ("mc-3", nb, "K-other", "K-rmsnorm", "pending", "", ""),
            ("mc-4", nb, "K-x", "K-y", "deferred", "x", "y"),
            ("mc-5", other, "K-rmsnorm", "K-z", "confirmed", "", ""),
        ):
            ins("concept_merge_candidates", "id,notebook_id,canonical_a,canonical_b,"
                "score,status,seed_a,seed_b,created_at,updated_at",
                (cid, notebook, a, b, 0.9, status, seed_a, seed_b, T0, T1))
    return world


def findings_payload(findings=()) -> dict:
    """The ``findings`` ledger payload that agrees with ``findings`` (the write
    entry checks ``persisted`` per rule against the rows written)."""
    counts: dict = {}
    for row in findings:
        counts[row[0]] = counts.get(row[0], 0) + 1
    return {
        "rules_version": RULES_VERSION,
        "rules": {rule: {"status": "ran", "total": counts.get(rule, 0),
                         "persisted": counts.get(rule, 0), "truncated": False}
                  for rule in ALL_RULES},
    }


def payloads(findings=()) -> dict:
    """One valid ledger batch (every required kind, cluster stamps and the
    ledger contract stamp the way the production path puts them on)."""
    batch = {kind: {"level": 0} for kind in ARTIFACT_KINDS}
    batch[ARTIFACT_COMMUNITY_EDGES] = {"level": 0, "communities": 2}
    batch[ARTIFACT_FINDINGS] = findings_payload(findings)
    return stamp_ledger_contract(stamp_cluster_seq(batch, 0, partition_rebuilt=True))


FINDINGS_V1 = (
    ("F2", "src-a|src-b", "source_pair", "Paper A ⇄ Paper B", ["src-a", "src-b"],
     "medium", 40, {"jaccard": 0.65, "shared": 12, "samples": ["RMSNorm"]}, 7, 3),
    ("F1", "src-a", "source", "Paper A 热设计", ["src-a"], "high", 84,
     {"shared_share": 0.0, "giant_share": None, "concepts": 84,
      "nested": {"b": [1, 2], "a": "x"}}, 5, 2),
    ("F5", "notebook", "notebook", "", [], "low", 1830, {}, 5, 2),
)
FINDINGS_V2 = (
    ("F1", "src-b", "source", "Paper B", ["src-b"], "high", 9, {"concepts": 12}, 8, 4),
)

EXPECTED_FINDINGS_V1 = [
    {"rule_id": "F1", "subject_key": "src-a", "subject_kind": "source",
     "subject_label": "Paper A 热设计", "subject_refs": ["src-a"], "severity": "high",
     "impact_objects": 84,
     "evidence": {"shared_share": 0.0, "giant_share": None, "concepts": 84,
                  "nested": {"b": [1, 2], "a": "x"}},
     "first_seen_kg_seq": 5, "first_seen_cluster_seq": 2},
    {"rule_id": "F2", "subject_key": "src-a|src-b", "subject_kind": "source_pair",
     "subject_label": "Paper A ⇄ Paper B", "subject_refs": ["src-a", "src-b"],
     "severity": "medium", "impact_objects": 40,
     "evidence": {"jaccard": 0.65, "shared": 12, "samples": ["RMSNorm"]},
     "first_seen_kg_seq": 7, "first_seen_cluster_seq": 3},
    {"rule_id": "F5", "subject_key": "notebook", "subject_kind": "notebook",
     "subject_label": "", "subject_refs": [], "severity": "low",
     "impact_objects": 1830, "evidence": {},
     "first_seen_kg_seq": 5, "first_seen_cluster_seq": 2},
]
EXPECTED_FINDINGS_V2 = [
    {"rule_id": "F1", "subject_key": "src-b", "subject_kind": "source",
     "subject_label": "Paper B", "subject_refs": ["src-b"], "severity": "high",
     "impact_objects": 9, "evidence": {"concepts": 12},
     "first_seen_kg_seq": 8, "first_seen_cluster_seq": 4},
]


def replace(rt, notebook_id: str, findings, prune=(), *, seq: int = 12) -> None:
    with rt.database.write() as db:
        rt.unified_kg.replace_kg_analysis_artifacts(
            db, notebook_id, seq, [], [], payloads(findings), T0,
            findings_rows=findings, prune_marks=prune,
        )


def finding_rows(rt, notebook_id: str):
    with rt.unified_kg.read_snapshot() as db:
        return rt.unified_kg.kg_analysis_finding_rows(db, notebook_id)


def mark_rows(rt, notebook_id: str):
    with rt.unified_kg.read_snapshot() as db:
        return rt.unified_kg.kg_analysis_mark_rows(db, notebook_id)


def write_mark(rt, notebook_id, rule_id, subject_key, kind, *, seq=3,
               actor="user-local", note="", now=T1) -> None:
    with rt.database.write() as db:
        rt.unified_kg.write_kg_analysis_mark(
            db, notebook_id, rule_id, subject_key, kind,
            at_kg_seq=seq, actor_user_id=actor, note=note, now=now,
        )


def clear_mark(rt, notebook_id, rule_id, subject_key, kind) -> bool:
    with rt.database.write() as db:
        return rt.unified_kg.clear_kg_analysis_mark(
            db, notebook_id, rule_id, subject_key, kind)


# -------------------------------------------------------- source states

SOURCE_STATE_REQUEST = ["src-a", "src-b", "src-k", "src-m", "src-o", "src-gone", "src-a"]
_ABSENT = {
    "exists": False, "title": "", "source_type": "", "status": "",
    "parse_status": "", "chunked_at": "", "last_extraction_at": "",
    "last_extraction_status": "", "created_at": "",
}
EXPECTED_SOURCE_STATES = {
    # er-a2 and er-a3 tie on created_at; the id breaks the tie, descending.
    "src-a": {"exists": True, "title": "Paper A", "source_type": "markdown",
              "status": "extracted", "parse_status": "parsed", "chunked_at": T0,
              "last_extraction_at": T2, "last_extraction_status": "failed",
              "created_at": T0},
    "src-b": {"exists": True, "title": "Paper B", "source_type": "markdown",
              "status": "parsing", "parse_status": "parsing", "chunked_at": "",
              "last_extraction_at": T1, "last_extraction_status": "running",
              "created_at": T1},
    # hidden synthetic sources: reported, titles blanked
    "src-k": {"exists": True, "title": "", "source_type": "knowhow",
              "status": "extracted", "parse_status": "parsed", "chunked_at": T0,
              "last_extraction_at": "", "last_extraction_status": "",
              "created_at": T0},
    "src-m": {"exists": True, "title": "", "source_type": "memory",
              "status": "extracted", "parse_status": "parsed", "chunked_at": T0,
              "last_extraction_at": "", "last_extraction_status": "",
              "created_at": T0},
    # another notebook's source and a deleted one both read as absent
    "src-o": dict(_ABSENT),
    "src-gone": dict(_ABSENT),
}


def source_states(rt, notebook_id: str, ids=SOURCE_STATE_REQUEST):
    with rt.unified_kg.read_snapshot() as db:
        return rt.unified_kg.kg_finding_source_states(db, notebook_id, ids)


# -------------------------------------------------------- display names

DISPLAY_NAME_PAIRS = [
    ("src-a", "K-rmsnorm"),
    ("src-k", "K-rmsnorm"),     # knowhow member: excluded
    ("src-m", "K-rmsnorm"),     # Memory member: excluded
    ("src-b", "K-rmsnorm"),     # only a deprecated member: nothing
    ("src-b", "ko-b1"),         # unclustered object: its own id is its canonical
    ("src-a", "ko-b1"),         # not src-a's object: nothing
    ("src-a", "ko-a4"),         # cluster row only in an unpublished generation
    ("src-gone", "K-rmsnorm"),  # orphan reference: kept, like source_canonical_rows
    ("src-a", "K-rmsnorm"),     # duplicate pair
]
EXPECTED_DISPLAY_NAMES = [
    ("src-a", "K-rmsnorm", "RMSNorm", 2),
    ("src-a", "K-rmsnorm", "RMS Norm", 1),
    ("src-a", "ko-a4", "Stale gen", 1),
    ("src-b", "ko-b1", "Layer Norm", 1),
    ("src-gone", "K-rmsnorm", "Orphan", 1),
]


def display_names(rt, notebook_id: str, pairs=DISPLAY_NAME_PAIRS):
    return rt.unified_kg.kg_finding_display_name_rows(notebook_id, pairs)


# -------------------------------------------------- merge candidate states

EXPECTED_DECIDED_STATES = [
    {"id": "mc-1", "canonical_a": "K-rmsnorm", "canonical_b": "K-rms norm",
     "status": "confirmed", "seed_a": "rmsnorm", "seed_b": "rms norm", "updated_at": T1},
    {"id": "mc-2", "canonical_a": "K-layer", "canonical_b": "K-rmsnorm",
     "status": "rejected", "seed_a": "layer", "seed_b": "", "updated_at": T1},
]
EXPECTED_ALL_STATES = [
    *EXPECTED_DECIDED_STATES,
    {"id": "mc-3", "canonical_a": "K-other", "canonical_b": "K-rmsnorm",
     "status": "pending", "seed_a": "", "seed_b": "", "updated_at": T1},
]


def candidate_states(rt, notebook_id: str, statuses, ids):
    with rt.unified_kg.read_snapshot() as db:
        return rt.governance.merge_candidate_states_for_canonicals(
            db, notebook_id, statuses, ids)


# ---------------------------------------------------------- code version

def state(rt, notebook_id: str) -> tuple[str, str]:
    with rt.unified_kg.read_snapshot() as db:
        row = rt.unified_kg.state_row(db, notebook_id)
    return row["cluster_input_version"], row["cluster_code_version"]


def set_versions(rt, notebook_id: str, input_version: str, code_version: str,
                 *, postgres: bool) -> None:
    p = "%s" if postgres else "?"
    with rt.database.write() as db:
        db.execute(
            f"UPDATE unified_kg_state SET cluster_input_version={p}, "
            f"cluster_code_version={p} WHERE notebook_id={p}",
            (input_version, code_version, notebook_id),
        )


def stamp(rt, notebook_id: str, input_version: str, code_version: str) -> bool:
    with rt.database.write() as db:
        return rt.unified_kg.stamp_cluster_code_version(
            db, notebook_id, input_version, code_version)


NAN_EVIDENCE = ("F1", "src-x", "source", "", [], "high", 1, {"v": math.nan}, 1, 1)
BAD_ROWS = (
    ("F1", "src-x", "source", "", [], "high", 1, {}, 1),            # too short
    ("", "src-x", "source", "", [], "high", 1, {}, 1, 1),            # empty rule
    ("F1", "src-x", "source", "", ["src", 3], "high", 1, {}, 1, 1),  # non-str ref
    ("F1", "src-x", "source", "", [], "high", 1, [], 1, 1),          # evidence list
    ("F1", "src-x", "source", "", [], "high", True, {}, 1, 1),       # bool impact
    ("F1", "src-x", "source", "", [], "high", -1, {}, 1, 1),         # negative
    ("F1", "src-x", "source", "", "src-x", "high", 1, {}, 1, 1),     # refs a str
)


# ======================================================================
# The checks. Each takes the repository, which backend it is, pytest's
# ``monkeypatch`` and the backend's ``unified_kg_store`` module (for the
# store-level caps), and asserts against the literals above.
# ======================================================================

def check_findings_replace_wholesale(repo, *, postgres: bool) -> None:
    """The detail is replaced wholesale with the ledger (整删整插), read back in
    primary-key order with the JSON columns intact; another notebook's rows are
    untouched by either write."""
    rt = repo._runtime
    world = seed(repo, postgres=postgres)
    replace(rt, world.nb, FINDINGS_V1)
    replace(rt, world.other, FINDINGS_V2)
    assert finding_rows(rt, world.nb) == EXPECTED_FINDINGS_V1
    replace(rt, world.nb, FINDINGS_V2)
    assert finding_rows(rt, world.nb) == EXPECTED_FINDINGS_V2
    replace(rt, world.nb, ())
    assert finding_rows(rt, world.nb) == []
    assert finding_rows(rt, world.other) == EXPECTED_FINDINGS_V2
    # the ledger rides the same statement batch
    with rt.unified_kg.read_snapshot() as db:
        assert set(rt.unified_kg.kg_analysis_artifact_rows(db, world.nb)) == set(
            payloads())


def check_findings_write_guards(repo, *, postgres: bool, monkeypatch, store_module) -> None:
    """Malformed rows and non-finite JSON are refused at the write entry and the
    whole transaction (ledger included) rolls back; the row cap is enforced on
    the write side and a table past it (only a hand edit can do that) fails the
    read instead of being truncated."""
    import pytest

    rt = repo._runtime
    world = seed(repo, postgres=postgres)
    replace(rt, world.nb, FINDINGS_V2)
    for bad in (*BAD_ROWS, NAN_EVIDENCE):
        with pytest.raises(ValueError):
            replace(rt, world.nb, FINDINGS_V1 + (bad,))
        assert finding_rows(rt, world.nb) == EXPECTED_FINDINGS_V2, bad
    # The write cap lives in the neutral write check both backends call
    # (``checked_finding_rows``); the read cap in each store's read.
    from app.repositories import kg_analysis_payloads

    monkeypatch.setattr(kg_analysis_payloads, "KG_FINDINGS_ROWS_MAX", 2)
    monkeypatch.setattr(store_module, "KG_FINDINGS_ROWS_MAX", 2)
    with pytest.raises(ValueError):
        replace(rt, world.nb, FINDINGS_V1)
    assert finding_rows(rt, world.nb) == EXPECTED_FINDINGS_V2
    replace(rt, world.nb, FINDINGS_V1[:2])
    assert len(finding_rows(rt, world.nb)) == 2
    p = "%s" if postgres else "?"
    with rt.database.write() as db:
        db.execute(
            "INSERT INTO kg_analysis_findings (notebook_id, rule_id, subject_key, "
            f"subject_kind, severity, first_seen_kg_seq, first_seen_cluster_seq) "
            f"VALUES ({p}, 'F9', 'hand-edit', 'source', 'low', 1, 1)",
            (world.nb,),
        )
    with pytest.raises(ValueError):
        finding_rows(rt, world.nb)


def check_marks(repo, *, postgres: bool, monkeypatch, store_module) -> None:
    """Upsert by primary key (a second write of the same key overwrites it),
    delete by primary key (reporting whether a row went), the kind vocabulary
    refused before the CHECK, ``prune_marks`` deleting exactly the named keys,
    and the mark cap enforced on NEW keys only."""
    import pytest

    rt = repo._runtime
    world = seed(repo, postgres=postgres)
    write_mark(rt, world.nb, "F1", "src-a", "dismissed", seq=3, note="looks fine")
    write_mark(rt, world.nb, "F1", "src-a", "dismissed", seq=4, note="still fine",
               now=T2)
    write_mark(rt, world.nb, "F5", "notebook", "relink_attempted", seq=9, actor="")
    write_mark(rt, world.nb, "F2", "src-a|src-b", "dismissed", seq=4)
    write_mark(rt, world.other, "F1", "src-o", "dismissed", seq=1)
    assert mark_rows(rt, world.nb) == [
        {"rule_id": "F1", "subject_key": "src-a", "kind": "dismissed",
         "at_kg_seq": 4, "actor_user_id": "user-local", "created_at": T2,
         "note": "still fine"},
        {"rule_id": "F2", "subject_key": "src-a|src-b", "kind": "dismissed",
         "at_kg_seq": 4, "actor_user_id": "user-local", "created_at": T1, "note": ""},
        {"rule_id": "F5", "subject_key": "notebook", "kind": "relink_attempted",
         "at_kg_seq": 9, "actor_user_id": "", "created_at": T1, "note": ""},
    ]
    with pytest.raises(ValueError):
        write_mark(rt, world.nb, "F1", "src-b", "bogus")
    assert clear_mark(rt, world.nb, "F1", "src-a", "dismissed") is True
    assert clear_mark(rt, world.nb, "F1", "src-a", "dismissed") is False
    with pytest.raises(ValueError):
        clear_mark(rt, world.nb, "F1", "src-a", "bogus")
    write_mark(rt, world.nb, "F1", "src-a", "dismissed", seq=5)
    replace(rt, world.nb, FINDINGS_V2,
            prune=[("F2", "src-a|src-b", "dismissed"), ("F3", "nothing", "dismissed")])
    assert [(m["rule_id"], m["kind"]) for m in mark_rows(rt, world.nb)] == [
        ("F1", "dismissed"), ("F5", "relink_attempted")]
    assert [m["subject_key"] for m in mark_rows(rt, world.other)] == ["src-o"]
    with pytest.raises(ValueError):
        replace(rt, world.nb, (), prune=[("F1", "src-a", "bogus")])
    assert len(mark_rows(rt, world.nb)) == 2
    monkeypatch.setattr(store_module, "KG_ANALYSIS_MARKS_MAX", 2)
    with pytest.raises(ValueError):
        write_mark(rt, world.nb, "F2", "src-b|src-c", "dismissed")
    write_mark(rt, world.nb, "F1", "src-a", "dismissed", seq=6)   # overwrite: fine
    assert mark_rows(rt, world.nb)[0]["at_kg_seq"] == 6
    p = "%s" if postgres else "?"
    with rt.database.write() as db:
        db.execute(
            "INSERT INTO kg_analysis_marks (notebook_id, rule_id, subject_key, kind, "
            f"at_kg_seq, created_at) VALUES ({p}, 'F9', 'hand-edit', 'dismissed', 1, {p})",
            (world.nb, T1),
        )
    with pytest.raises(ValueError):
        mark_rows(rt, world.nb)


def check_mark_cap_under_concurrency(repo, *, postgres: bool, monkeypatch,
                                    store_module) -> None:
    """Two writers, each adding a NEW mark key to a notebook one below the cap,
    on two connections. A barrier right after the count holds the first writer
    there until the second has counted too, or until it times out: with the
    count + insert serialized (PostgreSQL: the notebook advisory lock; SQLite:
    the write lock every write transaction holds) the second never gets to
    count while the first waits, the barrier times out, the first commits and
    the second then counts the cap and is refused. Without the serialization
    both count cap-1 and both insert: cap+1 rows, after which the mark read
    fails for good. Mutation anchor (PostgreSQL): drop the advisory lock."""
    import threading

    import pytest

    rt = repo._runtime
    world = seed(repo, postgres=postgres)
    monkeypatch.setattr(store_module, "KG_ANALYSIS_MARKS_MAX", 2)
    write_mark(rt, world.nb, "F1", "src-a", "dismissed")
    barrier = threading.Barrier(2, timeout=3)

    class Counted:
        """The writer's connection, pausing at the barrier after the count."""

        def __init__(self, db):
            self._db = db

        def execute(self, statement, *args, **kwargs):
            result = self._db.execute(statement, *args, **kwargs)
            if "SELECT COUNT(*) AS c FROM (SELECT 1 FROM kg_analysis_marks" in statement:
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    pass
            return result

        def __getattr__(self, name):
            return getattr(self._db, name)

    outcomes: dict[str, object] = {}

    def writer(key: str) -> None:
        try:
            with rt.database.write() as db:
                rt.unified_kg.write_kg_analysis_mark(
                    Counted(db), world.nb, "F2", key, "dismissed",
                    at_kg_seq=1, actor_user_id="user-local", note="", now=T1)
            outcomes[key] = "written"
        except ValueError as exc:
            outcomes[key] = exc
        except Exception as exc:  # noqa: BLE001 -- reported below
            outcomes[key] = repr(exc)

    threads = [threading.Thread(target=writer, args=(key,))
               for key in ("src-b|src-c", "src-c|src-d")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(outcomes) == ["src-b|src-c", "src-c|src-d"], outcomes
    written = [key for key, outcome in outcomes.items() if outcome == "written"]
    refused = [key for key, outcome in outcomes.items()
               if isinstance(outcome, ValueError)]
    assert len(written) == 1 and len(refused) == 1, outcomes
    assert len(mark_rows(rt, world.nb)) == 2
    with pytest.raises(ValueError):
        write_mark(rt, world.nb, "F3", "src-e", "dismissed")


def check_indexing_publish_clears_findings(repo, *, postgres: bool) -> None:
    """The indexing-pipeline publication (a staged rebuild generation going
    live) voids every notebook-derived KG product: the findings detail goes
    with its ledger, and cluster_code_version is emptied together with
    cluster_input_version. Both kinds of mark stay -- the publication moves
    kg_mutation_seq forward, never to 0, so a relink-attempt mark already reads
    as "content arrived since". Driven through the store itself with a
    notebook that has no sources (the empty staged generation is still a full
    publication). Mutation anchors: drop kg_analysis_findings from
    ``_clear_notebook_derived_kg``, or the ``cluster_code_version=''`` reset."""
    rt = repo._runtime
    p = "%s" if postgres else "?"
    notebook_id = repo.create_notebook(NotebookCreate(name="publish")).id
    pipeline = ("test.pipeline", "v1", "g1")
    job = rt.kg_build_jobs.create_job(notebook_id, "user-local", "rebuild", 0)
    with rt.database.write() as db:
        db.execute(
            f"UPDATE notebooks SET indexing_pipeline={p}, indexing_pipeline_version={p}, "
            f"indexing_pipeline_generation={p}, indexing_pipeline_job_id={p} "
            f"WHERE id={p}",
            (*pipeline, job["id"], notebook_id),
        )
    rt.kg_build_jobs.begin_indexing_pipeline_stage(
        job["id"], notebook_id, *pipeline, [])
    replace(rt, notebook_id, FINDINGS_V1)
    write_mark(rt, notebook_id, "F1", "src-a", "dismissed")
    write_mark(rt, notebook_id, "F5", "notebook", "relink_attempted")
    set_versions(rt, notebook_id, "v-in", "code-1", postgres=postgres)

    assert rt.kg_build_jobs.publish_indexing_pipeline_success(
        job["id"], notebook_id, *pipeline) is True

    assert finding_rows(rt, notebook_id) == []
    assert [(m["rule_id"], m["kind"]) for m in mark_rows(rt, notebook_id)] == [
        ("F1", "dismissed"), ("F5", "relink_attempted")]
    assert state(rt, notebook_id) == ("", "")


def check_source_states(repo, *, postgres: bool) -> None:
    rt = repo._runtime
    world = seed(repo, postgres=postgres)
    assert source_states(rt, world.nb) == EXPECTED_SOURCE_STATES
    assert source_states(rt, world.nb, []) == {}


def check_display_names(repo, *, postgres: bool) -> None:
    """Only the source's own usable objects' names, hidden-source objects left
    out (the canonical name itself comes from a knowhow object here and must
    not appear), orphan references kept, unclustered objects under their own
    id, a cluster row of an unpublished generation not counted; batching does
    not change the answer; refused inside a write transaction."""
    import pytest

    rt = repo._runtime
    world = seed(repo, postgres=postgres)
    names = display_names(rt, world.nb)
    assert names == EXPECTED_DISPLAY_NAMES
    assert all("knowhow" not in name and "memory" not in name
               for _s, _c, name, _n in names)
    assert display_names(rt, world.nb, []) == []
    assert display_names(rt, world.other) == []
    with rt.database.write():
        with pytest.raises(RuntimeError, match="写事务"):
            display_names(rt, world.nb)


def check_display_names_across_batches(repo, *, postgres: bool) -> None:
    """More canonicals than one statement batch: the answer is the same set."""
    rt = repo._runtime
    world = seed(repo, postgres=postgres)
    padding = [("src-a", f"K-none-{i:04d}") for i in range(1200)]
    assert display_names(rt, world.nb, padding + DISPLAY_NAME_PAIRS) == (
        EXPECTED_DISPLAY_NAMES)


MEMBER_NAME_REQUEST = ["ko-b1", "K-rmsnorm", "ko-a4", "ko-a1", "K-none", "K-rmsnorm"]
EXPECTED_MEMBER_NAMES = [
    # visible first, most objects first, then the name (code-point order)
    ("K-rmsnorm", "RMSNorm", True, 2),
    ("K-rmsnorm", "Orphan", True, 1),       # orphan reference: visible
    ("K-rmsnorm", "RMS Norm", True, 1),
    # hidden members are READ (their seeds decide), never displayed
    ("K-rmsnorm", "Dead", False, 1),                 # deprecated
    ("K-rmsnorm", "RMSNorm (knowhow)", False, 1),    # knowhow source
    ("K-rmsnorm", "RMSNorm (memory)", False, 1),     # Memory source
    # cluster row only in an unpublished generation: its own canonical
    ("ko-a4", "Stale gen", True, 1),
    ("ko-b1", "Layer Norm", True, 1),                # unclustered
    # "ko-a1" is a clustered MEMBER, not a canonical: nothing under its own id
]


def member_names(rt, notebook_id: str, ids=MEMBER_NAME_REQUEST):
    with rt.unified_kg.read_snapshot() as db:
        return rt.unified_kg.kg_finding_member_name_rows(db, notebook_id, ids)


def check_member_names(repo, *, postgres: bool) -> None:
    """Every member of each canonical (published generation only), hidden
    members included and flagged; unclustered objects under their own id;
    another notebook sees nothing; batching does not change the answer."""
    rt = repo._runtime
    world = seed(repo, postgres=postgres)
    assert member_names(rt, world.nb) == EXPECTED_MEMBER_NAMES
    assert member_names(rt, world.nb, []) == []
    assert member_names(rt, world.other) == []
    padding = [f"K-none-{i:04d}" for i in range(1200)]
    assert member_names(rt, world.nb, padding + MEMBER_NAME_REQUEST) == (
        EXPECTED_MEMBER_NAMES)


def check_candidate_states(repo, *, postgres: bool) -> None:
    import pytest

    rt = repo._runtime
    world = seed(repo, postgres=postgres)
    decided = ("confirmed", "rejected", "deferred")
    assert candidate_states(rt, world.nb, decided, ["K-rmsnorm"]) == (
        EXPECTED_DECIDED_STATES)
    assert candidate_states(
        rt, world.nb, (*decided, "pending"), ["K-rmsnorm", "K-rmsnorm"]
    ) == EXPECTED_ALL_STATES
    assert candidate_states(rt, world.nb, decided, ["K-y"]) == [
        {"id": "mc-4", "canonical_a": "K-x", "canonical_b": "K-y", "status": "deferred",
         "seed_a": "x", "seed_b": "y", "updated_at": T1}]
    assert candidate_states(rt, world.nb, decided, []) == []
    with pytest.raises(ValueError):
        candidate_states(rt, world.nb, ("confirmed",), [])


def check_code_version_stamp(repo, *, postgres: bool) -> None:
    """The skip path's stamp writes only under an equal input version and only
    when the value differs; the rebuild end-write stores it under its own
    generation guard; a KG delete empties it with the input version."""
    rt = repo._runtime
    world = seed(repo, postgres=postgres)
    assert state(rt, world.nb) == ("", "")
    set_versions(rt, world.nb, "v1", "", postgres=postgres)
    assert stamp(rt, world.nb, "v1", "code-1") is True
    assert state(rt, world.nb) == ("v1", "code-1")
    assert stamp(rt, world.nb, "v1", "code-1") is False      # already equal
    assert stamp(rt, world.nb, "v2", "code-2") is False      # input moved on
    assert state(rt, world.nb) == ("v1", "code-1")
    with rt.database.write() as db:
        rt.unified_kg.finish_rebuild_state(
            db, world.nb, "v3", 1, T2, 0, input_seq=0, cluster_code_version="code-3")
    assert state(rt, world.nb) == ("v3", "code-3")
    rt.knowledge_lifecycle.delete_notebook_kg(world.nb)
    assert state(rt, world.nb) == ("", "")
    # an emptied input version records no merge: nothing to stamp
    assert stamp(rt, world.nb, "", "code-4") is False
    assert state(rt, world.nb) == ("", "")

