"""Shared scenarios for the concept seed-space switch (S1) and the squash
candidate tier (P3) inside real rebuilds — design
docs/superpowers/specs/2026-10-19-kg-analysis-findings-design_zh.md §4.4,
§五 S1 「实施细化」.

``tests/test_kg_seed_space_switch.py`` runs every scenario on SQLite and
``tests/postgres/test_kg_seed_space_switch_pg.py`` on PostgreSQL. Backend-
neutral: ``?`` placeholders are rewritten for the PostgreSQL repository.

A library "before the upgrade" is built by the code before S1: concept seed
space 1 and cluster algorithm version 3 (``legacy_code``). The deployed code
(space 2, version 4) then rebuilds it, which is exactly what an operator's
first "更新分析" after the upgrade does. Stored decisions from before the
upgrade are written as the rows the migration left (``seed_version`` at its
default 1)."""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Dict, List

from app.models.schemas import NotebookCreate

NOW = "2026-10-21T00:00:00"


def is_postgres(repo) -> bool:
    return type(repo).__name__ == "PostgresRepository"


def sql(repo, statement: str) -> str:
    return statement.replace("?", "%s") if is_postgres(repo) else statement


def rows(repo, statement: str, params=()) -> List[dict]:
    with repo._connect() as db:
        return [dict(r) for r in db.execute(sql(repo, statement), params).fetchall()]


def write(repo, statement: str, params=()) -> None:
    with repo._write() as db:
        db.execute(sql(repo, statement), params)


@contextmanager
def legacy_code(monkeypatch):
    """Run the code before S1: concept seed space 1, algorithm version 3."""
    from app.services import kg_merge, knowledge_lifecycle

    seed, algo = knowledge_lifecycle.CONCEPT_SEED_VERSION, kg_merge.CLUSTER_ALGO_VERSION
    monkeypatch.setattr(knowledge_lifecycle, "CONCEPT_SEED_VERSION", 1)
    monkeypatch.setattr(kg_merge, "CLUSTER_ALGO_VERSION", 3)
    try:
        yield
    finally:
        monkeypatch.setattr(knowledge_lifecycle, "CONCEPT_SEED_VERSION", seed)
        monkeypatch.setattr(kg_merge, "CLUSTER_ALGO_VERSION", algo)


def notebook(repo, *sources: str) -> str:
    nb = repo.create_notebook(NotebookCreate(name="seed space")).id
    for source_id in sources:
        add_source(repo, nb, source_id)
    return nb


def add_source(repo, nb: str, source_id: str) -> None:
    write(repo, "INSERT INTO sources (id,notebook_id,title,source_type,status,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
          (source_id, nb, source_id, "md", "ready", NOW, NOW))


def concepts(repo, nb: str, source_id: str, *names: str, definition: str = "") -> None:
    payload = {"section_path": "1"}
    if definition:
        payload["definition"] = definition
    repo.store_kg(nb, source_id, [
        {"local_id": f"{source_id}-{i}", "object_type": "concept",
         "payload": {"name": name, **payload}, "evidence": []}
        for i, name in enumerate(names)
    ], [])


def fuse(repo, nb: str, source_id: str, *names: str) -> None:
    """A new source extracted and fused incrementally, as upload does."""
    add_source(repo, nb, source_id)
    concepts(repo, nb, source_id, *names)
    repo._runtime.knowledge_lifecycle.incremental_fuse_source(nb, source_id)


def canonical_of(repo, nb: str) -> Dict[str, str]:
    """Published concept clusters: object name -> canonical id."""
    found = rows(repo,
                 "SELECT ko.payload AS payload, cc.canonical_id AS cid "
                 "FROM concept_clusters cc JOIN knowledge_objects ko "
                 "ON ko.id = cc.member_object_id "
                 "WHERE cc.notebook_id=? AND cc.object_type='concept' AND cc.generation = "
                 "COALESCE((SELECT cluster_generation FROM unified_kg_state "
                 "WHERE notebook_id=?), 0)", (nb, nb))
    out: Dict[str, str] = {}
    for r in found:
        payload = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"])
        out.setdefault(payload["name"], r["cid"])
    return out


def published_space(repo, nb: str) -> int:
    return int(rows(repo, "SELECT concept_seed_version FROM unified_kg_state "
                          "WHERE notebook_id=?", (nb,))[0]["concept_seed_version"])


def decide(repo, nb: str, row_id: str, a: str, b: str, status: str, *,
           seeds: bool = True) -> None:
    """A decision stored before the upgrade: keys ``a`` / ``b`` in seed space 1,
    as ``seed_a`` / ``seed_b`` (or only as the canonical ids, the shape bridge
    candidates and early rows have)."""
    write(repo, "INSERT INTO concept_merge_candidates (id,notebook_id,canonical_a,"
                "canonical_b,seed_a,seed_b,score,status,reviewed_by,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
          (row_id, nb, "K-" + a, "K-" + b, a if seeds else "", b if seeds else "",
           0.9, status, "", NOW, NOW))


def decision_rows(repo, nb: str) -> List[tuple]:
    return sorted(
        (r["seed_a"], r["seed_b"], r["status"], int(r["seed_version"]))
        for r in rows(repo, "SELECT seed_a, seed_b, status, seed_version "
                            "FROM concept_merge_candidates WHERE notebook_id=? "
                            "AND status <> 'pending'", (nb,))
    )


def capture_events(repo, monkeypatch) -> list:
    events: list = []
    log = repo._runtime.knowledge_lifecycle.event_log
    real = log.emit

    def emit(payload):
        events.append(dict(payload))
        return real(payload)

    monkeypatch.setattr(log, "emit", emit)
    return events


def rebuild(repo, nb: str, *, force: bool = False) -> None:
    repo._runtime.knowledge_lifecycle.rebuild_unified_kg(nb, force=force)


# ================================================================ scenarios

def assert_the_published_space_switches_with_the_rebuild(repo, monkeypatch) -> None:
    """Placement follows the PUBLISHED space: before the library's first v2
    rebuild "Q_Former" still joins ``K-q former``; the rebuild publishes
    space 2 in its flip statement and joins every spelling into
    ``K-qformer``; after it, a new "Q-Former" lands there directly."""
    nb = notebook(repo, "s1", "s2")
    concepts(repo, nb, "s1", "Q-Former", "Q Former")
    concepts(repo, nb, "s2", "Qformer")
    with legacy_code(monkeypatch):
        rebuild(repo, nb)
    assert published_space(repo, nb) == 1
    assert canonical_of(repo, nb) == {
        "Q-Former": "K-q former", "Q Former": "K-q former", "Qformer": "K-qformer"}

    fuse(repo, nb, "s3", "Q_Former")                 # deployed code, library not rebuilt
    assert canonical_of(repo, nb)["Q_Former"] == "K-q former"
    candidates = rows(repo, "SELECT seed_version FROM concept_merge_candidates "
                            "WHERE notebook_id=?", (nb,))
    assert {int(r["seed_version"]) for r in candidates} <= {1}

    rebuild(repo, nb)                                # the upgrade's first "更新分析"
    assert published_space(repo, nb) == 2
    assert set(canonical_of(repo, nb).values()) == {"K-qformer"}
    fuse(repo, nb, "s4", "Q-Former")
    assert canonical_of(repo, nb)["Q-Former"] == "K-qformer"


def assert_a_split_confirmed_group_is_joined_back(repo, monkeypatch) -> dict:
    """"Llama 3.1" and "Llama-3-1" shared the v1 seed ``llama 3 1``; S1 gives
    them ``llama3 1`` and ``llama31``. A confirmed decision with "Meta Llama"
    becomes BOTH pairs, which keep all three in one canonical — the first pair
    alone would split "Llama-3-1" off. The decision row keeps its id and gains
    one row; the event reports it."""
    nb = notebook(repo, "s1", "s2", "s3")
    concepts(repo, nb, "s1", "Llama 3.1")
    concepts(repo, nb, "s2", "Llama-3-1")
    concepts(repo, nb, "s3", "Meta Llama")
    decide(repo, nb, "mc-old-confirmed", "llama 3 1", "meta llama", "confirmed")
    with legacy_code(monkeypatch):
        rebuild(repo, nb, force=True)
    assert len(set(canonical_of(repo, nb).values())) == 1
    events = capture_events(repo, monkeypatch)
    rebuild(repo, nb)
    assert len(set(canonical_of(repo, nb).values())) == 1, canonical_of(repo, nb)
    assert decision_rows(repo, nb) == [
        ("llama3 1", "metallama", "confirmed", 2),
        ("llama31", "metallama", "confirmed", 2),
    ]
    kept = rows(repo, "SELECT seed_a, canonical_a, canonical_b FROM concept_merge_candidates "
                      "WHERE id='mc-old-confirmed'")
    assert kept == [{"seed_a": "llama3 1", "canonical_a": "K-llama3 1",
                     "canonical_b": "K-metallama"}]
    [event] = [e for e in events if e["kind"] == "kg_merge_decisions_recomputed"]
    assert {k: event[k] for k in ("translated", "suspended", "decision_rows_added",
                                  "override_keys", "unresolved")} == {
        "translated": 1, "suspended": 0, "decision_rows_added": 1,
        "override_keys": 0, "unresolved": 0}
    return {"notebook_id": nb}


def assert_the_next_update_is_skipped(repo, monkeypatch) -> None:
    """The recompute grew the confirmed / rejected count by one; folded into
    the stored input version, the next "更新分析" with nothing changed is the
    skip path — not a second full re-merge."""
    nb = assert_a_split_confirmed_group_is_joined_back(repo, monkeypatch)["notebook_id"]
    lifecycle = repo._runtime.knowledge_lifecycle
    streamed: list = []
    real = lifecycle._stream_seed_reps
    monkeypatch.setattr(lifecycle, "_stream_seed_reps",
                        lambda *a, **k: streamed.append(a) or real(*a, **k))
    rebuild(repo, nb)
    assert streamed == [], "the second update re-merged"


def assert_a_rejection_the_new_seeds_would_collapse_keeps_the_old_seeds(
    repo, monkeypatch,
) -> None:
    """A user rejected merging "Q-Former" with "Qformer"; in space 2 both are
    ``qformer`` — the same seed is the same group, which no cannot-link can
    stop. Their objects keep their v1 seeds as ``~v1:<seed>`` (written into
    the decision row), every later rebuild applies it (read back from the
    decisions), and incremental placement does too."""
    nb = notebook(repo, "s1", "s2")
    concepts(repo, nb, "s1", "Q-Former")
    concepts(repo, nb, "s2", "Qformer")
    decide(repo, nb, "mc-old-rejected", "q former", "qformer", "rejected")
    with legacy_code(monkeypatch):
        rebuild(repo, nb, force=True)
    rebuild(repo, nb)
    assert canonical_of(repo, nb) == {
        "Q-Former": "K-~v1:q former", "Qformer": "K-~v1:qformer"}
    assert decision_rows(repo, nb) == [("~v1:q former", "~v1:qformer", "rejected", 2)]
    rebuild(repo, nb, force=True)                     # the override is read back
    assert canonical_of(repo, nb) == {
        "Q-Former": "K-~v1:q former", "Qformer": "K-~v1:qformer"}
    fuse(repo, nb, "s3", "Q Former")                  # v1 seed "q former": overridden
    assert canonical_of(repo, nb)["Q Former"] == "K-~v1:q former"
    pending = repo._runtime.knowledge_governance.pending_merges(nb)
    assert not [p for p in pending if {p["canonical_a"], p["canonical_b"]} == {
        "K-~v1:q former", "K-~v1:qformer"}], pending


def assert_rows_whose_objects_are_gone_stay_v1_and_are_retried(repo, monkeypatch) -> None:
    """A decision one side of which no current object carries is suspended:
    it stays in space 1 (an event counts it) and the next full rebuild tries
    again — once the object exists, it is translated."""
    nb = notebook(repo, "s1", "s2")
    concepts(repo, nb, "s1", "Phase Margin")
    decide(repo, nb, "mc-gone", "phase margin", "gain margin", "rejected")
    events = capture_events(repo, monkeypatch)
    rebuild(repo, nb, force=True)
    assert decision_rows(repo, nb) == [("phase margin", "gain margin", "rejected", 1)]
    [event] = [e for e in events if e["kind"] == "kg_merge_decisions_recomputed"]
    assert (event["translated"], event["suspended"]) == (0, 1)
    concepts(repo, nb, "s2", "Gain-Margin")
    rebuild(repo, nb, force=True)
    assert decision_rows(repo, nb) == [("phasemargin", "gainmargin", "rejected", 2)]


def assert_a_decision_keyed_by_canonical_ids_is_translated(repo, monkeypatch) -> None:
    """Bridge candidates and early rows carry no seeds: their keys are the
    canonical ids without ``K-``. They are recomputed the same way and get
    explicit v2 seeds."""
    nb = notebook(repo, "s1", "s2", "s3")
    concepts(repo, nb, "s1", "Llama 3.1")
    concepts(repo, nb, "s2", "Llama-3-1")
    concepts(repo, nb, "s3", "Meta Llama")
    decide(repo, nb, "mc-bridge", "llama 3 1", "meta llama", "confirmed", seeds=False)
    rebuild(repo, nb, force=True)
    assert len(set(canonical_of(repo, nb).values())) == 1, canonical_of(repo, nb)
    assert decision_rows(repo, nb) == [
        ("llama3 1", "metallama", "confirmed", 2),
        ("llama31", "metallama", "confirmed", 2),
    ]


def assert_a_row_decided_again_mid_rebuild_stays_v1(repo) -> None:
    """The store translates a row only while its keys and status are what the
    plan saw; a row decided again since (here: confirmed → rejected) stays v1
    for the next rebuild, which plans it afresh."""
    nb = notebook(repo)
    decide(repo, nb, "mc-x", "a b", "c", "confirmed")
    store = repo._runtime.governance
    with repo._write() as db:
        grew, done = store.apply_v1_decision_translation(
            db, nb, {"mc-x": [("ab", "c"), ("a1b", "c")]},
            {"mc-x": ("a b", "c", "rejected")}, NOW)
    assert (grew, done) == (0, [])
    assert decision_rows(repo, nb) == [("a b", "c", "confirmed", 1)]
    with repo._write() as db:
        grew, done = store.apply_v1_decision_translation(
            db, nb, {"mc-x": [("ab", "c"), ("a1b", "c")]},
            {"mc-x": ("a b", "c", "confirmed")}, NOW)
    assert (grew, done) == (1, ["mc-x"])
    assert decision_rows(repo, nb) == [("a1b", "c", "confirmed", 2), ("ab", "c", "confirmed", 2)]
    copied = rows(repo, "SELECT reviewed_by, score, created_at FROM concept_merge_candidates "
                        "WHERE notebook_id=? ORDER BY id", (nb,))
    assert len({(r["reviewed_by"], float(r["score"])) for r in copied}) == 1


def assert_the_squash_tier_proposes_spelling_variants(repo, monkeypatch) -> None:
    """"Llama 3.1" / "Llama 31" differ only in a punctuation-made space: S1
    keeps them apart on purpose, and the pending refresh proposes them as one
    star pair flagged ``spelling_variant`` (同名不同写法). A rejection is
    honoured by the next refresh; the Settings cap is reported when hit."""
    nb = notebook(repo, "s1", "s2", "s3")
    concepts(repo, nb, "s1", "Llama 3.1")
    concepts(repo, nb, "s2", "Llama 31", "Llama 31")
    concepts(repo, nb, "s3", "Transformer")
    rebuild(repo, nb)
    governance = repo._runtime.knowledge_governance
    pending = governance.pending_merges(nb)
    spelling = [p for p in pending if p["kind"] == "spelling_variant"]
    assert [(p["canonical_a"], p["canonical_b"]) for p in spelling] == [
        ("K-llama31", "K-llama3 1")]                     # anchor: most members
    assert all("seed_a" not in p for p in pending)
    governance.reject_merge(nb, spelling[0]["id"])
    rebuild(repo, nb, force=True)
    assert not [p for p in governance.pending_merges(nb) if p["kind"] == "spelling_variant"]

    nb2 = notebook(repo, "t1", "t2")
    concepts(repo, nb2, "t1", "Llama 3.1")
    concepts(repo, nb2, "t2", "Llama 31")
    events = capture_events(repo, monkeypatch)
    monkeypatch.setattr(repo._runtime.knowledge_lifecycle.settings,
                        "kg_merge_squash_candidates_max", 0)
    rebuild(repo, nb2)
    assert not [p for p in governance.pending_merges(nb2) if p["kind"] == "spelling_variant"]
    [capped] = [e for e in events if e["kind"] == "kg_merge_squash_candidates_capped"]
    assert (capped["total"], capped["kept"]) == (1, 0)


def assert_duplicate_blocking_follows_the_published_space(repo, monkeypatch) -> None:
    """查重 blocks by the seed of the notebook's PUBLISHED space: "Q-Former" and
    "Qformer" (same definition) are one candidate group only once the library
    publishes space 2."""
    nb = notebook(repo, "s1", "s2")
    text = "bridge module that queries frozen vision features for a language model"
    concepts(repo, nb, "s1", "Q-Former", definition=text)
    concepts(repo, nb, "s2", "Qformer", definition=text)
    governance = repo._runtime.knowledge_governance
    with legacy_code(monkeypatch):
        rebuild(repo, nb)
    assert governance.find_duplicates(nb, "concept") == []
    rebuild(repo, nb)
    groups = governance.find_duplicates(nb, "concept")
    assert len(groups) == 1 and len(groups[0].members) == 2, groups


SCENARIOS = {
    "published_space_switches": assert_the_published_space_switches_with_the_rebuild,
    "split_group_joined_back": assert_a_split_confirmed_group_is_joined_back,
    "next_update_skipped": assert_the_next_update_is_skipped,
    "rejection_collision_override": (
        assert_a_rejection_the_new_seeds_would_collapse_keeps_the_old_seeds),
    "suspended_rows_retried": assert_rows_whose_objects_are_gone_stay_v1_and_are_retried,
    "canonical_keyed_row_translated": assert_a_decision_keyed_by_canonical_ids_is_translated,
    "squash_tier": assert_the_squash_tier_proposes_spelling_variants,
    "duplicates_follow_space": assert_duplicate_blocking_follows_the_published_space,
}
