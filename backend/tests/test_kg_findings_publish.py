"""The KG findings list end to end through `rebuild_communities` (SQLite):
computed in the precompute, published in the boards' transaction
(design docs/superpowers/specs/2026-10-19-kg-analysis-findings-design_zh.md
§7.1 ⑧ / §7.4 / §8). The PostgreSQL twin of the world-level cases is
``tests/postgres/test_kg_findings_publish_pg.py`` (same world, same expected
rows: ``tests/kg_findings_world.py``)."""
from __future__ import annotations

import json
import re
from contextlib import contextmanager

import pytest

from app.core.config import Settings
from app.domain.kg_analysis_contracts import (
    ARTIFACT_COMMUNITY_EDGES,
    ARTIFACT_FINDINGS,
    ARTIFACT_KINDS,
    ARTIFACT_SOURCE_PROFILES,
    BOARD_DEPENDENT_ARTIFACT_KINDS,
    CLUSTER_SEQ_PAYLOAD_KEY,
    KG_FINDINGS_ROWS_MAX,
    LEDGER_CONTRACT_KEY,
    MARK_DISMISSED,
    MARK_RELINK_ATTEMPTED,
)
from app.domain.kg_findings_contracts import (
    FINDING_RULES,
    MAX_PERSISTED_FINDINGS,
    RULES_VERSION,
)
from app.domain.kg_merge_seed import concept_seed_v1
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.kg_analysis import (
    ABSENCE_UNEXPECTED,
    ABSENCE_UPGRADE_PENDING,
)
from app.services.kg_analysis_precompute import analysis_ledger_is_current
from app.services.kg_findings_view import relink_view
from app.services.sqlite_repository import SQLiteRepository
from tests.kg_findings_world import (
    KNOWHOW_HIDDEN_SEED_NAME,
    KNOWHOW_SECRET,
    SECRETS,
    seed_world,
)
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repository = SQLiteRepository(Settings())
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    return repository


def _ledger(repo, notebook_id: str) -> dict:
    store = repo._runtime.unified_kg
    with store.read_snapshot() as db:
        return store.kg_analysis_artifact_rows(db, notebook_id)


def _rows(repo, notebook_id: str) -> list:
    store = repo._runtime.unified_kg
    with store.read_snapshot() as db:
        return store.kg_analysis_finding_rows(db, notebook_id)


def _marks(repo, notebook_id: str) -> list:
    store = repo._runtime.unified_kg
    with store.read_snapshot() as db:
        return store.kg_analysis_mark_rows(db, notebook_id)


def _keys(rows) -> list:
    return [(row["rule_id"], row["subject_key"]) for row in rows]


def _overview(repo, notebook_id: str):
    return repo._runtime.kg_analysis.overview(notebook_id)


def _view(overview, kind: str):
    return next(view for view in overview.artifacts if view.kind == kind)


def _seq(repo, notebook_id: str) -> tuple:
    store = repo._runtime.unified_kg
    with store.read_snapshot() as db:
        row = store.state_row(db, notebook_id)
    return int(row["kg_mutation_seq"]), int(row["cluster_mutation_seq"])


# The world's expected detail (both backends; PG twin compares the same list).
EXPECTED_KEYS = [
    ("F1", "src-off"),
    ("F2", "src-dup-a|src-dup-b"),
    ("F3", "src-broken"),
    ("F4", "llama31"),
    ("F5", "notebook"),
]


def test_the_world_publishes_one_finding_per_rule(repo):
    """Every rule fires once on the synthetic world; S1 counts its narrow group;
    the canonical name taken from a knowhow object never reaches a row or the
    payload (design §8.4) — the F4 label is the visible member's own spelling."""
    world = seed_world(repo, postgres=False)
    repo._runtime.knowledge_lifecycle.rebuild_communities(world.notebook_id)
    rows = _rows(repo, world.notebook_id)
    payload = _ledger(repo, world.notebook_id)[ARTIFACT_FINDINGS]["payload"]

    assert sorted(_keys(rows)) == EXPECTED_KEYS
    assert payload["rules_version"] == RULES_VERSION
    assert payload["rules"]["S1"]["narrow_groups"] == 1
    f4 = next(row for row in rows if row["rule_id"] == "F4")
    assert f4["subject_label"] == "Llama 3.1"
    assert f4["evidence"]["classes"] == [["K-llama 3 1"], ["K-llama 31"]]
    assert world.canonical_names["K-llama 3 1"] == KNOWHOW_SECRET
    # the Mistral group exists on canonical names only (one side is named by a
    # knowhow object): dropped on the visible names, counted, never shown
    assert payload["rules"]["F4"]["groups_unreproducible"] == 1
    published = json.dumps([rows, payload], ensure_ascii=False)
    assert [secret for secret in SECRETS if secret in published] == []
    # first publication: everything is new at this build's two generations
    kg_seq, cluster_seq = _seq(repo, world.notebook_id)
    assert {(r["first_seen_kg_seq"], r["first_seen_cluster_seq"]) for r in rows} == {
        (kg_seq, cluster_seq)}
    assert payload["comparison"]["comparable"] is False
    assert payload["comparison"]["reason"] == "no_previous"
    # S3 numbers: the component-only ones in findings, the partition ones in
    # the board-dependent community_edges payload
    edges_payload = _ledger(repo, world.notebook_id)[ARTIFACT_COMMUNITY_EDGES]["payload"]
    assert payload["structure"]["island_components"] == 1
    assert {"boards_in_giant", "boards_equal_component"} <= set(edges_payload)
    assert "boards_in_giant" not in payload["structure"]
    assert edges_payload["boards_equal_component"] >= 1      # the off-topic island


def test_a_decision_keyed_by_a_hidden_member_seed_keeps_f4_quiet(repo):
    """Stored decisions match by the v1 seeds of EVERY member, hidden ones
    included (design §4.4): a rejection recorded against the knowhow member's
    seed — a spelling no visible member has — still means "not the same thing".
    Mutation anchor: let the member-seed read keep only visible members."""
    world = seed_world(repo, postgres=False)
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO concept_merge_candidates(id,notebook_id,canonical_a,canonical_b,"
            "score,status,seed_a,seed_b,created_at,updated_at) "
            "VALUES ('mc-1',?,'K-gone-a','K-gone-b',0.9,'rejected',?,?,?,?)",
            (world.notebook_id, concept_seed_v1(KNOWHOW_HIDDEN_SEED_NAME),
             concept_seed_v1("Llama 31"), "2026-10-19", "2026-10-19"))
    repo._runtime.knowledge_lifecycle.rebuild_communities(world.notebook_id)
    payload = _ledger(repo, world.notebook_id)[ARTIFACT_FINDINGS]["payload"]
    assert ("F4", "llama31") not in _keys(_rows(repo, world.notebook_id))
    assert payload["rules"]["F4"]["groups_decided_apart"] == 1


def test_a_second_build_inherits_first_seen_and_compares(repo):
    """``finalize_findings`` in the publish transaction: rows still reported keep
    the generation they were first seen at; the comparison counts them."""
    world = seed_world(repo, postgres=False)
    lifecycle = repo._runtime.knowledge_lifecycle
    lifecycle.rebuild_communities(world.notebook_id)
    first = {(r["rule_id"], r["subject_key"]): r for r in _rows(repo, world.notebook_id)}
    with repo._runtime.database.write() as db:
        db.execute(
            "UPDATE unified_kg_state SET kg_mutation_seq = kg_mutation_seq + 3 "
            "WHERE notebook_id=?", (world.notebook_id,))
    lifecycle.rebuild_communities(world.notebook_id)
    second = _rows(repo, world.notebook_id)
    payload = _ledger(repo, world.notebook_id)[ARTIFACT_FINDINGS]["payload"]

    assert sorted(_keys(second)) == EXPECTED_KEYS
    for row in second:
        old = first[(row["rule_id"], row["subject_key"])]
        assert row["first_seen_kg_seq"] == old["first_seen_kg_seq"]
    comparison = payload["comparison"]
    assert comparison["comparable"] is True
    assert (comparison["resolved"], comparison["persisting"], comparison["new"]) == (
        0, len(EXPECTED_KEYS), 0)
    assert comparison["previous_built_at_seq"] == _seq(repo, world.notebook_id)[0] - 3


# PRODUCT_TABLES in test_kg_analysis_precompute + the findings detail.
_PUBLISHED = ("kg_community_edges", "kg_source_profiles", "kg_analysis_artifacts",
              "kg_analysis_findings")
_DML_TABLE = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|DELETE\s+FROM|UPDATE)"
    r"\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.I,
)


def test_findings_publish_atomically_with_boards(repo, monkeypatch):
    """The findings detail is written by exactly ONE write transaction — the one
    that flips the board generation and rewrites the ledger and the other two
    detail tables (design §7.1 ⑧). Mutation anchor: write the findings rows in a
    separate ``with self._write()`` after the publish → two transactions touch
    the findings table / the flip transaction lacks it."""
    world = seed_world(repo, postgres=False)
    database = repo._runtime.database
    original_write = database.write
    per_transaction: list = []
    watched = {*_PUBLISHED, "unified_kg_state", "kg_analysis_marks"}

    @contextmanager
    def tracing_write(**kwargs):
        with original_write(**kwargs) as db:
            touched: set = set()
            per_transaction.append(touched)
            db.set_trace_callback(
                lambda statement: touched.update(
                    watched.intersection(_DML_TABLE.findall(statement))))
            try:
                yield db
            finally:
                db.set_trace_callback(None)

    monkeypatch.setattr(database, "write", tracing_write)
    repo._runtime.knowledge_lifecycle.rebuild_communities(world.notebook_id)

    touching = [t for t in per_transaction if "kg_analysis_findings" in t]
    assert len(touching) == 1, per_transaction
    assert touching[0] >= {*_PUBLISHED, "unified_kg_state"}, (
        f"the findings detail is not in the flip + ledger transaction: {touching[0]}")
    assert sorted(_keys(_rows(repo, world.notebook_id))) == EXPECTED_KEYS


def test_backfill_findings_cluster_seq_is_int(repo):
    """The backfill path (graph fresh, ledger missing) cannot date the board
    partition — the two board-dependent kinds record ``None`` — but findings do
    not depend on the partition and are stamped with the integer merge
    generation (design §7.1)."""
    world = seed_world(repo, postgres=False)
    lifecycle = repo._runtime.knowledge_lifecycle
    lifecycle.rebuild_communities(world.notebook_id)
    with repo._runtime.database.write() as db:
        db.execute("DELETE FROM kg_analysis_artifacts WHERE notebook_id=?",
                   (world.notebook_id,))
    lifecycle.rebuild_communities(world.notebook_id)          # backfill only
    ledger = _ledger(repo, world.notebook_id)
    _kg_seq, cluster_seq = _seq(repo, world.notebook_id)

    assert set(ledger) == set(ARTIFACT_KINDS)
    for kind in BOARD_DEPENDENT_ARTIFACT_KINDS:
        assert ledger[kind]["payload"][CLUSTER_SEQ_PAYLOAD_KEY] is None, kind
    stamp = ledger[ARTIFACT_FINDINGS]["payload"][CLUSTER_SEQ_PAYLOAD_KEY]
    assert isinstance(stamp, int) and not isinstance(stamp, bool)
    assert stamp == cluster_seq
    assert {row["first_seen_cluster_seq"] for row in _rows(repo, world.notebook_id)} == {
        cluster_seq}


def test_upgrade_ledger_self_heals(repo):
    """A library deployed before findings existed: a ledger whose rows carry no
    ``ledger_contract`` stamp and no findings row. The read side says "not
    generated since the upgrade" (grey), not "missing" (red); the gate sees an
    incomplete ledger, so the next NON-force rebuild backfills findings and
    stamps every row."""
    world = seed_world(repo, postgres=False)
    lifecycle = repo._runtime.knowledge_lifecycle
    lifecycle.rebuild_communities(world.notebook_id)
    with repo._runtime.database.write() as db:
        db.execute("DELETE FROM kg_analysis_artifacts WHERE notebook_id=? AND kind=?",
                   (world.notebook_id, ARTIFACT_FINDINGS))
        db.execute("DELETE FROM kg_analysis_findings WHERE notebook_id=?",
                   (world.notebook_id,))
        for row in db.execute(
            "SELECT kind, payload FROM kg_analysis_artifacts WHERE notebook_id=?",
            (world.notebook_id,),
        ).fetchall():
            payload = json.loads(row["payload"])
            payload.pop(LEDGER_CONTRACT_KEY)
            db.execute(
                "UPDATE kg_analysis_artifacts SET payload=? WHERE notebook_id=? AND kind=?",
                (json.dumps(payload), world.notebook_id, row["kind"]))

    view = _view(_overview(repo, world.notebook_id), ARTIFACT_FINDINGS)
    assert view.present is False and view.absence == ABSENCE_UPGRADE_PENDING
    kg_seq, cluster_seq = _seq(repo, world.notebook_id)
    assert not analysis_ledger_is_current(
        _ledger(repo, world.notebook_id), kg_seq, cluster_seq, has_boards=True)

    lifecycle.rebuild_communities(world.notebook_id)           # not forced

    ledger = _ledger(repo, world.notebook_id)
    assert set(ledger) == set(ARTIFACT_KINDS)
    assert all(entry["payload"][LEDGER_CONTRACT_KEY] == 1 for entry in ledger.values())
    assert sorted(_keys(_rows(repo, world.notebook_id))) == EXPECTED_KEYS
    overview = _overview(repo, world.notebook_id)
    assert _view(overview, ARTIFACT_FINDINGS).absence is None
    assert overview.ledger_state == "complete"


def test_a_stamped_ledger_without_findings_is_a_real_absence(repo):
    """The grey "upgrade pending" needs EVERY present row to predate the stamp:
    a ledger written by this version that lacks findings is genuinely broken."""
    world = seed_world(repo, postgres=False)
    repo._runtime.knowledge_lifecycle.rebuild_communities(world.notebook_id)
    with repo._runtime.database.write() as db:
        db.execute("DELETE FROM kg_analysis_artifacts WHERE notebook_id=? AND kind=?",
                   (world.notebook_id, ARTIFACT_FINDINGS))
    view = _view(_overview(repo, world.notebook_id), ARTIFACT_FINDINGS)
    assert view.absence == ABSENCE_UNEXPECTED
    # one unstamped row among stamped ones is not "every row predates the stamp"
    with repo._runtime.database.write() as db:
        row = db.execute(
            "SELECT payload FROM kg_analysis_artifacts WHERE notebook_id=? AND kind=?",
            (world.notebook_id, ARTIFACT_COMMUNITY_EDGES)).fetchone()
        payload = json.loads(row["payload"])
        payload.pop(LEDGER_CONTRACT_KEY)
        db.execute(
            "UPDATE kg_analysis_artifacts SET payload=? WHERE notebook_id=? AND kind=?",
            (json.dumps(payload), world.notebook_id, ARTIFACT_COMMUNITY_EDGES))
    view = _view(_overview(repo, world.notebook_id), ARTIFACT_FINDINGS)
    assert view.absence == ABSENCE_UNEXPECTED


def test_findings_built_on_older_rules_are_stale_and_rebuilt(repo):
    """``rules_version`` is the findings' third freshness line (§3.2): a ledger
    row computed by other rules is never current — the read side reports it
    stale and the gate reruns the precompute (otherwise "更新分析" would say
    "nothing to update" right after new rules ship)."""
    world = seed_world(repo, postgres=False)
    lifecycle = repo._runtime.knowledge_lifecycle
    lifecycle.rebuild_communities(world.notebook_id)
    with repo._runtime.database.write() as db:
        row = db.execute(
            "SELECT payload FROM kg_analysis_artifacts WHERE notebook_id=? AND kind=?",
            (world.notebook_id, ARTIFACT_FINDINGS)).fetchone()
        payload = json.loads(row["payload"])
        payload["rules_version"] = RULES_VERSION - 1
        db.execute(
            "UPDATE kg_analysis_artifacts SET payload=? WHERE notebook_id=? AND kind=?",
            (json.dumps(payload), world.notebook_id, ARTIFACT_FINDINGS))
    kg_seq, cluster_seq = _seq(repo, world.notebook_id)
    assert _view(_overview(repo, world.notebook_id), ARTIFACT_FINDINGS).freshness.stale is True
    assert not analysis_ledger_is_current(
        _ledger(repo, world.notebook_id), kg_seq, cluster_seq, has_boards=True)

    lifecycle.rebuild_communities(world.notebook_id)           # not forced

    rebuilt = _ledger(repo, world.notebook_id)[ARTIFACT_FINDINGS]["payload"]
    assert rebuilt["rules_version"] == RULES_VERSION
    # the previous build ran other rules: never compared, first-seen not inherited
    assert rebuilt["comparison"]["reason"] == "rules_version_changed"
    assert _view(_overview(repo, world.notebook_id), ARTIFACT_FINDINGS).freshness.stale is False


def test_a_full_unified_rebuild_publishes_findings_and_a_skip_stamps_the_merge_code(repo):
    """The hooks this feature hangs on, reached the way production reaches them:
    ``rebuild_unified_kg`` (split into phase helpers) publishes the findings
    through its derived tail's ``rebuild_communities`` — a tail that is
    fail-open, so a broken wire would otherwise go unnoticed — and an unchanged
    rerun takes the skip path, which stamps the merge code the overview's
    ``merge_code`` compares."""
    world = seed_world(repo, postgres=False)
    lifecycle = repo._runtime.knowledge_lifecycle
    events: list = []
    original_emit = lifecycle.event_log.emit
    lifecycle.event_log.emit = lambda event: (events.append(event), original_emit(event))[1]
    try:
        assert lifecycle.rebuild_unified_kg(world.notebook_id, force=True) > 0
    finally:
        lifecycle.event_log.emit = original_emit
    assert [e for e in events if str(e.get("kind", "")).endswith("_failed")] == []
    ledger = _ledger(repo, world.notebook_id)
    kg_seq, cluster_seq = _seq(repo, world.notebook_id)
    assert ARTIFACT_FINDINGS in ledger
    assert analysis_ledger_is_current(ledger, kg_seq, cluster_seq, has_boards=True)
    assert ledger[ARTIFACT_FINDINGS]["payload"]["rules_version"] == RULES_VERSION
    assert ("F5", "notebook") in _keys(_rows(repo, world.notebook_id))

    with repo._runtime.database.write() as db:                     # an older merge code
        db.execute("UPDATE unified_kg_state SET cluster_code_version="
                   "'algo=1;embed_dim=1;runtime_dim=0' WHERE notebook_id=?",
                   (world.notebook_id,))
    assert _overview(repo, world.notebook_id).merge_code.matches is False
    before = _seq(repo, world.notebook_id)
    lifecycle.rebuild_unified_kg(world.notebook_id)               # unchanged: skip path
    assert _seq(repo, world.notebook_id) == before                # nothing recomputed
    assert _overview(repo, world.notebook_id).merge_code.matches is True


def test_zero_board_still_writes_findings(repo):
    """No relation at all → no board, no source profiles (legal absence), but
    findings are required: the object scan still runs and F5 reports every
    usable object as isolated."""
    from tests.kg_findings_world import T0

    repo.settings.community_min_size = 2
    notebook_id = repo.create_notebook(NotebookCreate(name="flat")).id
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
            "created_at,updated_at) VALUES ('src-x',?,'X','markdown','extracted',"
            "'parsed',?,?)", (notebook_id, T0, T0))
        for k in range(4):
            db.execute(
                "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
                "evidence,source_id,created_at,updated_at) VALUES (?,?,'claim','approved',"
                "?, '[]','src-x',?,?)",
                (f"ko-flat-{k}", notebook_id, json.dumps({"name": f"c{k}"}), T0, T0))
        db.execute("UPDATE unified_kg_state SET kg_mutation_seq=3 WHERE notebook_id=?",
                   (notebook_id,))

    assert repo._runtime.knowledge_lifecycle.rebuild_communities(notebook_id) == 0
    ledger = _ledger(repo, notebook_id)
    assert ARTIFACT_FINDINGS in ledger and ARTIFACT_SOURCE_PROFILES not in ledger
    assert ledger[ARTIFACT_COMMUNITY_EDGES]["payload"]["communities"] == 0
    rows = _rows(repo, notebook_id)
    assert _keys(rows) == [("F5", "notebook")]
    assert rows[0]["impact_objects"] == 4
    assert ledger[ARTIFACT_FINDINGS]["payload"]["structure"]["graph_nodes"] == 0


def test_relink_mark_with_zero_edges(repo):
    """"Relink" leaves a durable mark at the generation it finished on — also
    when it added nothing (that is exactly the "running it again changes
    nothing" case F5 needs). A run that raises leaves no mark."""
    world = seed_world(repo, postgres=False)
    lifecycle = repo._runtime.knowledge_lifecycle
    result = lifecycle.relink_notebook_kg(world.notebook_id)
    assert result["edges_added"] == 0
    kg_seq, _cluster = _seq(repo, world.notebook_id)
    marks = _marks(repo, world.notebook_id)
    assert [(m["rule_id"], m["subject_key"], m["kind"], m["at_kg_seq"]) for m in marks] == [
        ("F5", "notebook", MARK_RELINK_ATTEMPTED, kg_seq)]


def _relinkable_pair(repo, world, tag: str) -> None:
    """A claim and a concept of one world source sharing an evidence element and
    no relation: relink links them (shared element, ``about``)."""
    from tests.kg_findings_world import T0

    with repo._runtime.database.write() as db:
        for suffix, object_type in (("c", "claim"), ("k", "concept")):
            db.execute(
                "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
                "evidence,source_id,created_at,updated_at) VALUES (?,?,?,'approved',?,?,?,?,?)",
                (f"ko-{tag}-{suffix}", world.notebook_id, object_type,
                 json.dumps({"name": f"{tag} {object_type}"}),
                 json.dumps([{"element_id": f"el-{tag}"}]), world.sources[0], T0, T0))


def _relink_mark(repo, notebook_id: str) -> tuple:
    marks = _marks(repo, notebook_id)
    kg_seq, _cluster = _seq(repo, notebook_id)
    view = relink_view(marks, kg_seq)
    return kg_seq, view["at_kg_seq"], view["may_change"]


def test_relink_mark_lands_on_the_generation_its_own_writes_reached(repo):
    """No other writer: the seq moved only by the run's own per-source commits,
    so the run saw exactly the current content — the mark is the current
    generation and running it again cannot link more."""
    world = seed_world(repo, postgres=False)
    _relinkable_pair(repo, world, "iso")
    started, _cluster = _seq(repo, world.notebook_id)
    result = repo._runtime.knowledge_lifecycle.relink_notebook_kg(world.notebook_id)
    assert result["edges_added"] >= 1
    assert _relink_mark(repo, world.notebook_id) == (started + 1, started + 1, False)


def test_relink_mark_falls_back_to_its_start_when_an_ingest_lands_mid_run(
        repo, monkeypatch):
    """codex #941 R2: the maintenance slot does not hold back ordinary ingestion.
    A KG write landing after a source's partition was relinked is content the run
    never read — marking the final generation would claim "running it again is
    useless" (F5 → relink_spent). The mark falls back to the generation the run
    started on, so ``may_change`` stays true."""
    from tests.kg_findings_world import T0

    world = seed_world(repo, postgres=False)
    _relinkable_pair(repo, world, "iso")
    lifecycle = repo._runtime.knowledge_lifecycle
    original = lifecycle._relink_one_source
    landed: list = []

    def one_source_then_an_ingest(notebook_id, source_id, written):
        counts = original(notebook_id, source_id, written)
        if not landed:                  # an ordinary ingest commits mid-run
            with repo._runtime.database.write() as db:
                repo._runtime.unified_kg.mark_dirty(db, notebook_id, T0)
            landed.append(source_id)
        return counts

    monkeypatch.setattr(lifecycle, "_relink_one_source", one_source_then_an_ingest)
    started, _cluster = _seq(repo, world.notebook_id)
    result = lifecycle.relink_notebook_kg(world.notebook_id)
    assert result["edges_added"] >= 1 and landed
    # own bump + the ingest's; the mark stays at the start
    assert _relink_mark(repo, world.notebook_id) == (started + 2, started, True)


def test_a_failed_relink_leaves_no_mark(repo, monkeypatch):
    world = seed_world(repo, postgres=False)
    lifecycle = repo._runtime.knowledge_lifecycle

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(lifecycle, "_relink_one_source", boom)
    with pytest.raises(RuntimeError, match="boom"):
        lifecycle.relink_notebook_kg(world.notebook_id)
    assert _marks(repo, world.notebook_id) == []


def test_publish_prunes_dismissals_whose_subject_is_gone(repo):
    """A dismissal survives every build that still reports its subject; one whose
    subject left the detail is pruned in the publish transaction; the relink
    mark is never pruned (design §8.3)."""
    world = seed_world(repo, postgres=False)
    lifecycle = repo._runtime.knowledge_lifecycle
    lifecycle.rebuild_communities(world.notebook_id)
    store = repo._runtime.unified_kg
    with repo._runtime.database.write() as db:
        for rule_id, key, kind in (("F1", "src-off", MARK_DISMISSED),
                                   ("F2", "src-gone|src-off", MARK_DISMISSED),
                                   ("F5", "notebook", MARK_RELINK_ATTEMPTED)):
            store.write_kg_analysis_mark(
                db, world.notebook_id, rule_id, key, kind, at_kg_seq=5,
                actor_user_id="", note="", now="2026-10-19T09:00:00")
    lifecycle.rebuild_communities(world.notebook_id, force=True)
    assert [(m["rule_id"], m["subject_key"], m["kind"])
            for m in _marks(repo, world.notebook_id)] == [
        ("F1", "src-off", MARK_DISMISSED), ("F5", "notebook", MARK_RELINK_ATTEMPTED)]


def _dismiss(repo, notebook_id, rule_id, key):
    assert repo._runtime.kg_analysis.dismiss_finding(
        notebook_id, rule_id, key, actor_user_id="u-1") is True


def _dismissed_keys(repo, notebook_id):
    return sorted((m["rule_id"], m["subject_key"]) for m in _marks(repo, notebook_id)
                  if m["kind"] == MARK_DISMISSED)


def _drop_objects(repo, notebook_id, where: str, params=()):
    """Remove objects (and what hangs off them) the way a KG delete or a reparse in
    flight leaves a source: the source row stays, its knowledge is gone."""
    with repo._runtime.database.write() as db:
        ids = [row["id"] for row in db.execute(
            f"SELECT id FROM knowledge_objects WHERE notebook_id=? AND {where}",
            (notebook_id, *params)).fetchall()]
        for object_id in ids:
            db.execute("DELETE FROM knowledge_relations WHERE source_object_id=? "
                       "OR target_object_id=?", (object_id, object_id))
            db.execute("DELETE FROM concept_clusters WHERE member_object_id=?", (object_id,))
            db.execute("DELETE FROM knowledge_objects WHERE id=?", (object_id,))
        db.execute("UPDATE unified_kg_state SET kg_mutation_seq = kg_mutation_seq + 1 "
                   "WHERE notebook_id=?", (notebook_id,))


def test_a_dismissal_survives_a_kg_delete_awaiting_re_extraction(repo):
    """Failure mode 1 of "not in the detail ⇒ prune": the knowledge graph was
    deleted (or a source's knowledge dropped) and not re-extracted yet when the
    analysis is updated. The source still exists, so its dismissal stays; it
    goes once the source itself is deleted."""
    world = seed_world(repo, postgres=False)
    lifecycle = repo._runtime.knowledge_lifecycle
    lifecycle.rebuild_communities(world.notebook_id)
    _dismiss(repo, world.notebook_id, "F1", "src-off")
    _drop_objects(repo, world.notebook_id, "source_id='src-off'")
    lifecycle.rebuild_communities(world.notebook_id, force=True)
    assert ("F1", "src-off") not in _keys(_rows(repo, world.notebook_id))
    assert _dismissed_keys(repo, world.notebook_id) == [("F1", "src-off")]
    # counted only while the subject is in the current detail
    assert repo._runtime.kg_analysis.overview(
        world.notebook_id).findings_summary.counts["dismissed"] == 0
    with repo._runtime.database.write() as db:
        db.execute("DELETE FROM sources WHERE id='src-off'")
    lifecycle.rebuild_communities(world.notebook_id, force=True)
    assert _dismissed_keys(repo, world.notebook_id) == []


def test_a_dismissal_survives_a_reparse_in_flight(repo):
    """Failure mode 2: the dismissed source is being reparsed (its old knowledge
    cleared, the new not extracted yet) when a build publishes."""
    world = seed_world(repo, postgres=False)
    lifecycle = repo._runtime.knowledge_lifecycle
    lifecycle.rebuild_communities(world.notebook_id)
    _dismiss(repo, world.notebook_id, "F3", "src-broken")
    with repo._runtime.database.write() as db:
        db.execute("UPDATE sources SET status='parsing' WHERE id='src-broken'")
    _drop_objects(repo, world.notebook_id, "source_id='src-broken'")
    lifecycle.rebuild_communities(world.notebook_id, force=True)
    assert ("F3", "src-broken") not in _keys(_rows(repo, world.notebook_id))
    assert _dismissed_keys(repo, world.notebook_id) == [("F3", "src-broken")]


def test_a_dismissal_survives_its_rule_being_truncated(repo, monkeypatch):
    """Failure mode 3: the rule hit its row cap — a subject can drop out of the
    persisted window by ranking alone. Nothing of a truncated rule is pruned,
    even when a source of the subject is gone (the rule saw only part)."""
    from app.services import kg_findings

    world = seed_world(repo, postgres=False)
    lifecycle = repo._runtime.knowledge_lifecycle
    lifecycle.rebuild_communities(world.notebook_id)
    _dismiss(repo, world.notebook_id, "F2", "src-dup-a|src-dup-b")
    with repo._runtime.database.write() as db:
        db.execute("DELETE FROM sources WHERE id='src-dup-b'")
    monkeypatch.setattr(kg_findings, "MAX_PERSISTED_FINDINGS", 0)
    lifecycle.rebuild_communities(world.notebook_id, force=True)
    payload = _ledger(repo, world.notebook_id)[ARTIFACT_FINDINGS]["payload"]
    assert payload["rules"]["F2"]["truncated"] is True
    assert _dismissed_keys(repo, world.notebook_id) == [("F2", "src-dup-a|src-dup-b")]


def test_a_dismissal_survives_its_rule_being_skipped(repo):
    """Failure mode 4: fewer than two comparable sources left — F1/F2 are
    skipped for this build; a skipped rule prunes nothing."""
    world = seed_world(repo, postgres=False)
    lifecycle = repo._runtime.knowledge_lifecycle
    lifecycle.rebuild_communities(world.notebook_id)
    _dismiss(repo, world.notebook_id, "F2", "src-dup-a|src-dup-b")
    _drop_objects(repo, world.notebook_id, "source_id <> 'src-dup-a'")
    with repo._runtime.database.write() as db:
        db.execute("DELETE FROM sources WHERE id='src-dup-b'")
    lifecycle.rebuild_communities(world.notebook_id, force=True)
    payload = _ledger(repo, world.notebook_id)[ARTIFACT_FINDINGS]["payload"]
    assert payload["rules"]["F2"]["status"] == "skipped"
    assert _dismissed_keys(repo, world.notebook_id) == [("F2", "src-dup-a|src-dup-b")]


def test_f4_and_f5_dismissals_survive_publication(repo):
    """F4 / F5 dismissals are never pruned at publication (an F4 group the user
    then rejected leaves the detail; the dismissal stays until undone)."""
    world = seed_world(repo, postgres=False)
    lifecycle = repo._runtime.knowledge_lifecycle
    lifecycle.rebuild_communities(world.notebook_id)
    _dismiss(repo, world.notebook_id, "F4", "llama31")
    _dismiss(repo, world.notebook_id, "F5", "notebook")
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO concept_merge_candidates(id,notebook_id,canonical_a,canonical_b,"
            "score,status,seed_a,seed_b,created_at,updated_at) "
            "VALUES ('mc-r',?,'K-llama 3 1','K-llama 31',0.9,'rejected','llama 3 1',"
            "'llama 31','2026-10-19','2026-10-19')", (world.notebook_id,))
    lifecycle.rebuild_communities(world.notebook_id, force=True)
    assert ("F4", "llama31") not in _keys(_rows(repo, world.notebook_id))
    assert _dismissed_keys(repo, world.notebook_id) == [("F4", "llama31"), ("F5", "notebook")]


def test_a_failed_relink_mark_does_not_fail_the_relink(repo, monkeypatch):
    """The edges are committed before the mark is written: a failing mark write
    must not turn a successful relink into a failure. It is reported as an
    event carrying the exception CLASS only, never its text."""
    world = seed_world(repo, postgres=False)
    lifecycle = repo._runtime.knowledge_lifecycle
    events: list = []
    original_emit = lifecycle.event_log.emit
    monkeypatch.setattr(lifecycle.event_log, "emit",
                        lambda event: (events.append(event), original_emit(event))[1])

    def boom(*_args, **_kwargs):
        raise RuntimeError("secret-path /var/db relation kg_analysis_marks")

    monkeypatch.setattr(repo._runtime.unified_kg, "write_kg_analysis_mark", boom)
    result = lifecycle.relink_notebook_kg(world.notebook_id)
    assert result["edges_added"] == 0
    failed = [{k: e[k] for k in ("kind", "notebook_id", "error_class")}
              for e in events if e.get("kind") == "kg_relink_mark_failed"]
    assert failed == [{"kind": "kg_relink_mark_failed", "notebook_id": world.notebook_id,
                       "error_class": "RuntimeError"}]
    assert "secret" not in json.dumps(events, default=str)


def test_the_graph_scan_counts_relation_rows_per_source(repo):
    """Step ① feeds F3b: every `community_graph_rows` row counted under its own
    ``kr.source_id`` — self-loops included (a relation both of whose ends merged
    into one canonical is still a relation the source produced). The world
    chains each source's concepts, so every source has (concepts - 1) rows;
    one extra self-loop row is added to src-off."""
    world = seed_world(repo, postgres=False)
    with repo._runtime.database.write() as db:
        loop = db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=? AND source_id='src-off' "
            "ORDER BY id LIMIT 1", (world.notebook_id,)).fetchone()["id"]
        db.execute(
            "INSERT INTO knowledge_relations(id,notebook_id,source_id,source_object_id,"
            "target_object_id,edge_type,evidence,created_at) "
            "VALUES ('kr-loop',?,'src-off',?,?,'supports','[]','2026-10-19')",
            (world.notebook_id, loop, loop))
    _names, _can2idx, _idx2can, _edges, relations, _scanned_at = (
        repo._runtime.knowledge_lifecycle._scan_canonical_graph(world.notebook_id))
    assert relations == {
        "src-core-0": 12, "src-core-1": 12, "src-core-2": 13, "src-core-3": 13,
        "src-off": 12, "src-dup-a": 11, "src-dup-b": 11, "src-broken": 11,
    }


def test_the_world_snapshot_matches_the_fixture_both_backends_share(repo):
    """T10 parity: SQLite publishes and serves exactly ``EXPECTED_SNAPSHOT`` — the
    same file ``tests/postgres/test_kg_findings_publish_pg.py`` compares the
    PostgreSQL run against, so the two backends are held to byte-identical rows,
    payload, summary and pages, not to "something plausible each"."""
    from tests.kg_findings_world import EXPECTED_SNAPSHOT, world_snapshot

    world = seed_world(repo, postgres=False)
    repo._runtime.knowledge_lifecycle.rebuild_communities(world.notebook_id)
    expected = json.loads(EXPECTED_SNAPSHOT.read_text(encoding="utf-8"))
    assert world_snapshot(repo, world.notebook_id) == expected


def test_persisted_findings_fit_the_storage_cap():
    """The engine's per-rule cap times the rules that persist rows must fit the
    store's per-notebook row cap: the write entry fails hard above it, and the
    publish read relies on reading the whole detail."""
    assert MAX_PERSISTED_FINDINGS * len(FINDING_RULES) <= KG_FINDINGS_ROWS_MAX
