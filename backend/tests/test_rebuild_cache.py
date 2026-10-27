"""Tests for the skip-when-unchanged gate on rebuild_unified_kg (perf fix).

rebuild_unified_kg re-clusters the whole KG on every call. This gate computes an
O(1) content-version of the clustering INPUTS (objects, decided merge pairs,
embeddings, embed_dim) and, when unchanged since the last rebuild, skips the
entire recompute and returns the cached cluster count. The cardinal rule is
correctness: a stale clustering result must NEVER be served, so the version must
change whenever any clustering input changes.

The tests spy on repo._stream_seed_reps (the first heavy step of the recompute)
to assert whether a real rebuild ran or the gate short-circuited.
"""
import json

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository, _now
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))  # inject; no real model loads (lazy)
    return r


def _concept(local_id, name, span="span"):
    return {
        "local_id": local_id,
        "object_type": "concept",
        "payload": {"name": name, "section_path": "1"},
        "evidence": [{
            "source_id": "s", "source_title": "D",
            "element_id": "e", "element_type": "p", "location_label": "1",
            "quoted_span": span, "confidence": 1.0,
        }],
    }


def _make_kg(repo, names=("MOSFET", "cascode")):
    """A notebook with a few concepts across two sources (some share a
    normalized name so a canonical is multi-member)."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, "s1", [_concept(f"a{i}", n, f"{n}-a") for i, n in enumerate(names)], [])
    repo.store_kg(nb.id, "s2", [_concept(f"b{i}", n.lower(), f"{n}-b") for i, n in enumerate(names)], [])
    return nb


def _spy_stream(repo, monkeypatch):
    """Wrap _stream_seed_reps so we can assert whether the recompute ran."""
    calls = {"n": 0}
    orig = repo._runtime.knowledge_lifecycle._stream_seed_reps

    def _wrapped(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(repo._runtime.knowledge_lifecycle, "_stream_seed_reps", _wrapped)
    return calls


def _cluster_rows(repo, nb_id):
    # 批 3·W2:按 published 代读(镜像读者契约)。翻转后的退休代行刻意留
    # 一轮宽限(D-W2-7,由下一轮预回收清),裸读全代会把它们数进来。
    with repo._connect() as db:
        return db.execute(
            "SELECT object_type, member_object_id, canonical_id, canonical_name "
            "FROM concept_clusters WHERE notebook_id=? "
            "AND generation = COALESCE((SELECT cluster_generation "
            "FROM unified_kg_state WHERE notebook_id=?),0) "
            "ORDER BY object_type, member_object_id, canonical_id",
            (nb_id, nb_id)).fetchall()


# --- 1. version sensitivity -------------------------------------------------

def test_version_changes_when_concept_added(repo):
    nb = _make_kg(repo)
    v0 = repo._runtime.knowledge_lifecycle._cluster_input_version(nb.id)
    repo.store_kg(nb.id, "s3", [_concept("c0", "bandgap", "bandgap-c")], [])
    v1 = repo._runtime.knowledge_lifecycle._cluster_input_version(nb.id)
    assert v0 != v1


def test_version_changes_when_embedding_replaced(repo):
    nb = _make_kg(repo)
    v0 = repo._runtime.knowledge_lifecycle._cluster_input_version(nb.id)
    # Add a brand-new embedding row (count changes) for a synthetic object id.
    with repo._write() as db:
        db.execute(
            "INSERT OR REPLACE INTO knowledge_embeddings (object_id, notebook_id, vector, created_at) "
            "VALUES (?,?,?,?)",
            ("ko-synthetic", nb.id, json.dumps([0.0] * 16), _now()),
        )
    v1 = repo._runtime.knowledge_lifecycle._cluster_input_version(nb.id)
    assert v0 != v1


def test_version_changes_when_decided_pair_recorded(repo):
    nb = _make_kg(repo)
    repo.rebuild_unified_kg(nb.id)  # populates pending candidates
    v0 = repo._runtime.knowledge_lifecycle._cluster_input_version(nb.id)
    # Record a confirmed decided pair (mirrors decided_pairs' WHERE).
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO concept_merge_candidates "
            "(id,notebook_id,canonical_a,canonical_b,score,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?, 'confirmed', ?, ?)",
            ("mc-decided", nb.id, "K-foo", "K-bar", 0.9, now, now),
        )
    v1 = repo._runtime.knowledge_lifecycle._cluster_input_version(nb.id)
    assert v0 != v1


def test_version_stable_across_rebuild(repo):
    """rebuild writes only concept_clusters + pending candidates, neither of which
    is in the signature, so the version is identical before and after a rebuild."""
    nb = _make_kg(repo)
    before = repo._runtime.knowledge_lifecycle._cluster_input_version(nb.id)
    repo.rebuild_unified_kg(nb.id)
    after = repo._runtime.knowledge_lifecycle._cluster_input_version(nb.id)
    assert before == after


def test_version_excludes_pending_candidates(repo):
    """Pending merge candidates (written by rebuild) must NOT move the version;
    only confirmed/rejected decisions do."""
    nb = _make_kg(repo)
    v0 = repo._runtime.knowledge_lifecycle._cluster_input_version(nb.id)
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO concept_merge_candidates "
            "(id,notebook_id,canonical_a,canonical_b,score,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?, 'pending', ?, ?)",
            ("mc-pending", nb.id, "K-foo", "K-bar", 0.9, now, now),
        )
    v1 = repo._runtime.knowledge_lifecycle._cluster_input_version(nb.id)
    assert v0 == v1


# --- 2. skip when unchanged -------------------------------------------------

def test_second_rebuild_skips_when_unchanged(repo, monkeypatch):
    nb = _make_kg(repo)
    first = repo.rebuild_unified_kg(nb.id)
    rows_before = [tuple(r) for r in _cluster_rows(repo, nb.id)]

    calls = _spy_stream(repo, monkeypatch)
    second = repo.rebuild_unified_kg(nb.id)

    assert calls["n"] == 0                       # recompute short-circuited
    assert second == first                       # same cluster count returned
    rows_after = [tuple(r) for r in _cluster_rows(repo, nb.id)]
    assert rows_after == rows_before             # concept_clusters untouched
    assert rows_after                            # (sanity) clusters exist


# --- 3. invalidation --------------------------------------------------------

def test_rebuild_recomputes_after_input_change(repo, monkeypatch):
    nb = _make_kg(repo)
    repo.rebuild_unified_kg(nb.id)

    # Add a new concept -> version changes -> next rebuild must recompute.
    repo.store_kg(nb.id, "s3", [_concept("c0", "bandgap", "bandgap-c")], [])

    calls = _spy_stream(repo, monkeypatch)
    repo.rebuild_unified_kg(nb.id)
    assert calls["n"] >= 1                        # real recompute ran


# --- 4. force bypass --------------------------------------------------------

def test_force_bypasses_gate(repo, monkeypatch):
    nb = _make_kg(repo)
    repo.rebuild_unified_kg(nb.id)

    calls = _spy_stream(repo, monkeypatch)
    repo.rebuild_unified_kg(nb.id, force=True)    # unchanged inputs, but forced
    assert calls["n"] >= 1                        # recompute ran despite no change


# --- 5. migration -----------------------------------------------------------

# --- 6. first-ever rebuild never skips --------------------------------------

def test_first_rebuild_never_skips(repo, monkeypatch):
    nb = _make_kg(repo)
    # No stored version yet (no prior rebuild) -> must recompute.
    calls = _spy_stream(repo, monkeypatch)
    repo.rebuild_unified_kg(nb.id)
    assert calls["n"] >= 1


# --- 7. IN-PLACE edit at fixed cardinality, same wall-clock second -----------
# These are the adversarial-review repros: at _now()'s 1-second resolution a
# COUNT+MAX(timestamp) version misses an in-place edit whose timestamp lands in
# the same second as the prior rebuild (COUNT unchanged, MAX pinned by another
# row). The fix is a monotonic kg_mutation_seq bumped by _mark_unified_kg_dirty,
# which moves deterministically regardless of clock granularity. We pin _now to a
# CONSTANT so no timestamp can move — only the seq can save us.

def _freeze_now(monkeypatch, const="2020-01-01T00:00:00"):
    """Pin _now() to a constant EVERYWHERE it's used for versioning/mutation."""
    import app.services.sqlite_repository as repo_mod
    monkeypatch.setattr(repo_mod, "_now", lambda: const)


def _concept_db_id(repo, nb_id, name):
    with repo._connect() as db:
        row = db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=? AND object_type='concept' "
            "AND json_extract(payload,'$.name')=?", (nb_id, name)).fetchone()
    return row["id"] if row else None


def test_rename_same_second_moves_version_and_recomputes(repo, monkeypatch):
    from app.models.schemas import KnowledgeUpdate

    _freeze_now(monkeypatch)  # every write shares one timestamp — no MAX signal
    # Two DISTINCT concept names -> two separate canonicals.
    nb = _make_kg(repo, names=("alpha", "beta"))
    repo.rebuild_unified_kg(nb.id)
    rows_before = [tuple(r) for r in _cluster_rows(repo, nb.id)]
    n_canon_before = len({r[2] for r in rows_before})   # canonical_id set
    assert n_canon_before == 2                          # alpha, beta distinct
    v0 = repo._runtime.knowledge_lifecycle._cluster_input_version(nb.id)

    # RENAME beta -> alpha (both sources' "beta" objects). Now every concept
    # normalizes to "alpha" -> the two canonicals MUST collapse to one.
    for name in ("beta", "beta"):  # both s1 and s2 carry a "beta"
        oid = _concept_db_id(repo, nb.id, "beta")
        if oid is None:
            break
        repo.update_knowledge(nb.id, oid, KnowledgeUpdate(payload={"name": "alpha", "section_path": "1"}))

    v1 = repo._runtime.knowledge_lifecycle._cluster_input_version(nb.id)
    assert v1 != v0, "version must move on an in-place rename (same-second)"

    calls = _spy_stream(repo, monkeypatch)
    second = repo.rebuild_unified_kg(nb.id)             # force=False (automatic path)
    assert calls["n"] >= 1, "gate must NOT skip after a rename — else stale serve"
    rows_after = [tuple(r) for r in _cluster_rows(repo, nb.id)]
    n_canon_after = len({r[2] for r in rows_after})
    assert n_canon_after == 1, "renamed concepts must collapse to one canonical"
    assert second == 1
    assert rows_after != rows_before                    # clustering actually changed


def test_decision_flip_same_second_moves_version_and_recomputes(repo, monkeypatch):
    _freeze_now(monkeypatch)
    nb = _make_kg(repo)
    repo.rebuild_unified_kg(nb.id)                      # writes pending candidates

    # Seed a CONFIRMED decision, rebuild so it's baked into the stored version.
    with repo._write() as db:
        db.execute(
            "INSERT INTO concept_merge_candidates "
            "(id,notebook_id,canonical_a,canonical_b,score,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?, 'confirmed', ?, ?)",
            ("mc-flip", nb.id, "K-foo", "K-bar", 0.9, "2020-01-01T00:00:00", "2020-01-01T00:00:00"),
        )
    repo.rebuild_unified_kg(nb.id, force=True)          # bake decision into version
    v0 = repo._runtime.knowledge_lifecycle._cluster_input_version(nb.id)

    # FLIP confirmed -> rejected via the real mutator (same-second, COUNT fixed).
    repo.reject_merge(nb.id, "mc-flip")
    v1 = repo._runtime.knowledge_lifecycle._cluster_input_version(nb.id)
    assert v1 != v0, "version must move on a confirmed<->rejected flip (same-second)"

    calls = _spy_stream(repo, monkeypatch)
    repo.rebuild_unified_kg(nb.id)                      # automatic path
    assert calls["n"] >= 1, "gate must NOT skip after a decision flip — else stale"


# --- 8. migration: kg_mutation_seq column -----------------------------------

def test_mutation_seq_increments_on_dirty(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))

    def _seq():
        with repo._connect() as db:
            r = db.execute("SELECT kg_mutation_seq FROM unified_kg_state WHERE notebook_id=?",
                           (nb.id,)).fetchone()
        return int(r["kg_mutation_seq"]) if r else 0

    assert _seq() == 0
    repo._mark_unified_kg_dirty(nb.id)
    assert _seq() == 1
    repo._mark_unified_kg_dirty(nb.id)
    assert _seq() == 2


def test_rebuild_preserves_mutation_seq(repo):
    """rebuild clears dirty=0 but must NOT reset/bump kg_mutation_seq (else the
    gate goes permanently dead or a mutation is lost)."""
    nb = _make_kg(repo)                                 # store_kg -> several dirty bumps
    with repo._connect() as db:
        seq_before = int(db.execute(
            "SELECT kg_mutation_seq FROM unified_kg_state WHERE notebook_id=?",
            (nb.id,)).fetchone()["kg_mutation_seq"])
    assert seq_before >= 1
    repo.rebuild_unified_kg(nb.id)
    with repo._connect() as db:
        row = db.execute(
            "SELECT dirty, kg_mutation_seq FROM unified_kg_state WHERE notebook_id=?",
            (nb.id,)).fetchone()
    assert row["dirty"] == 0                            # rebuild cleared dirty
    assert int(row["kg_mutation_seq"]) == seq_before    # seq preserved (not reset/bumped)


# --- 9. algorithm-version component: code-only semantics change invalidates ----
# A change to clustering SEMANTICS that lives purely in code (Unicode-safe _norm,
# the empty-seed sentinel, guardrails) leaves every DATA-derived component of the
# version identical — kg_mutation_seq, the three COUNTs and embed_dim are all
# unchanged. Without an algorithm-version component, a deployed library clicking
# 刷新图谱 (force=False) would be silently skipped and keep its pre-fix
# mega-clusters. kg_merge.CLUSTER_ALGO_VERSION is folded into
# _cluster_input_version so any bump of the constant moves the version. We model a
# code-only semantics change by patching the constant where the version READS it
# (module attribute), matching how _cluster_input_version imports it at call time.

def test_version_changes_when_algo_version_bumped(repo, monkeypatch):
    """Bumping kg_merge.CLUSTER_ALGO_VERSION changes the version string even
    though every data-derived component is byte-for-byte identical."""
    import app.services.kg_merge as kg_merge

    nb = _make_kg(repo)
    v0 = repo._runtime.knowledge_lifecycle._cluster_input_version(nb.id)
    monkeypatch.setattr(kg_merge, "CLUSTER_ALGO_VERSION",
                        kg_merge.CLUSTER_ALGO_VERSION + 1)
    v1 = repo._runtime.knowledge_lifecycle._cluster_input_version(nb.id)
    assert v0 != v1


def test_algo_version_bump_forces_recompute(repo, monkeypatch):
    """End-to-end: after a rebuild bakes the version in, an unchanged automatic
    rebuild skips (baseline); bumping the algo version constant must then make the
    force=False gate recompute — else deployed libraries keep pre-fix clusters."""
    import app.services.kg_merge as kg_merge

    nb = _make_kg(repo)
    repo.rebuild_unified_kg(nb.id)                       # bake inputs into stored version

    calls = _spy_stream(repo, monkeypatch)

    # Baseline: unchanged inputs -> automatic (force=False) path skips recompute.
    repo.rebuild_unified_kg(nb.id)
    assert calls["n"] == 0

    # A code-only clustering-semantics change, modeled as a constant bump where
    # _cluster_input_version reads it. The gate must now refuse to skip.
    monkeypatch.setattr(kg_merge, "CLUSTER_ALGO_VERSION",
                        kg_merge.CLUSTER_ALGO_VERSION + 1)
    repo.rebuild_unified_kg(nb.id)                       # force=False, but algo bumped
    assert calls["n"] >= 1                               # real recompute ran (not skipped)


# --- 10. the merge code component (SQLite v97 / PostgreSQL 0077) ---------------
# unified_kg_state.cluster_code_version publishes the code part of the input
# version (design 2026-10-19 §五 S4): the analysis panel must be able to tell
# "the last merge ran under older merge rules" without recomputing the input
# version's counts. It is written at the rebuild end-write and on the skip path.

def _code_version(repo, notebook_id):
    with repo._runtime.unified_kg.read_snapshot() as db:
        row = repo._runtime.unified_kg.state_row(db, notebook_id)
    return row["cluster_input_version"], row["cluster_code_version"]


def test_cluster_input_version_hash_is_byte_identical(monkeypatch):
    """Golden: moving the three code/config components behind
    kg_merge.cluster_code_components left the hash exactly as it was. The digests
    below were computed by the pre-change implementation over these fixed
    facts; any change to the parts, their order or their spelling breaks them --
    and with them every deployed library's skip gate (one full re-merge each)."""
    from contextlib import contextmanager
    from types import SimpleNamespace

    import app.services.kg_merge as kg_merge
    from app.services.knowledge_lifecycle import KnowledgeLifecycleService

    # The digests pin the FORMULA; they were computed with algorithm version 3.
    # PR-4's bump to 4 changes the value folded in (one re-merge per library,
    # on purpose), not the formula.
    assert kg_merge.CLUSTER_ALGO_VERSION == 4
    monkeypatch.setattr(kg_merge, "CLUSTER_ALGO_VERSION", 3)

    class Facts:
        def cluster_input_facts(self, db, notebook_id, *, exclude_emb_count=False):
            return (17, 230, 9, 412)

    @contextmanager
    def connect():
        yield None

    stub = SimpleNamespace(
        _connect=connect, unified_kg=Facts(),
        settings=SimpleNamespace(embed_dim=1024, embed_runtime_dim=512))
    version = KnowledgeLifecycleService._cluster_input_version
    assert version(stub, "nb-golden") == "c79f39c5d55aa529d1a60b8bd90dcfe38648b065"
    # The LLM-checkpoint namespace leaves dec_c out since PR-4 (S1 step 5: the
    # decision recompute adds rows before the flip, and a crash between the
    # two must resume on the checkpoints). Its digest moved once -- that only
    # re-keys checkpoints, which the algorithm bump re-keys anyway; the
    # skip-gate digests above and below are unchanged.
    assert version(stub, "nb-golden", exclude_emb_count=True) == (
        "94b6cf4a859a114234b2f537c63c050362230981")
    stub.settings = SimpleNamespace(embed_dim=16, embed_runtime_dim=0)
    assert version(stub, "nb-golden") == "8b872b5fcf47fb3ef83eb2bc6fcf1c0d99b4bbbf"
    # A rebuild's end-write hashes its ENTRY facts with the decision recompute's
    # row growth folded into dec_c: exactly the version the next rebuild reads
    # once those rows exist (S1 step 5).
    assert version(stub, "nb-golden", facts=(17, 230, 9, 412), dec_growth=2) == \
        version(SimpleNamespace(_connect=connect, settings=stub.settings,
                                unified_kg=SimpleNamespace(
                                    cluster_input_facts=lambda db, nb, **kw: (17, 230, 11, 412))),
                "nb-golden")


def test_cluster_code_version_reads_the_same_three_components(repo, monkeypatch):
    import app.services.kg_merge as kg_merge

    settings = repo._runtime.knowledge_lifecycle.settings
    assert kg_merge.cluster_code_version(settings) == (
        f"algo={kg_merge.CLUSTER_ALGO_VERSION};embed_dim=16;runtime_dim="
        f"{int(getattr(settings, 'embed_runtime_dim', 0) or 0)}")
    monkeypatch.setattr(kg_merge, "CLUSTER_ALGO_VERSION",
                        kg_merge.CLUSTER_ALGO_VERSION + 1)
    assert kg_merge.cluster_code_version(settings).startswith(
        f"algo={kg_merge.CLUSTER_ALGO_VERSION};")


def test_rebuild_end_write_stores_the_code_version(repo):
    import app.services.kg_merge as kg_merge

    lifecycle = repo._runtime.knowledge_lifecycle
    nb = _make_kg(repo)
    assert _code_version(repo, nb.id) == ("", "")
    lifecycle.rebuild_unified_kg(nb.id)
    stored_input, stored_code = _code_version(repo, nb.id)
    assert stored_input == lifecycle._cluster_input_version(nb.id)
    assert stored_code == kg_merge.cluster_code_version(lifecycle.settings)


def test_skip_path_stamps_code_version(repo, monkeypatch):
    """A library upgraded to v97 carries '' until something writes the column.
    The inputs-unchanged skip path proves the stored merge was built under the
    running code (the stored input version folds the code component in) and
    stamps it -- otherwise the panel could never reach "up to date" without a
    forced re-merge. A skip that finds the value already right writes nothing.
    Mutation anchors: drop the stamp from ``_settle_skip_path`` -> the first
    assertion pair goes red; drop its equality pre-check -> the zero-write
    assertion goes red."""
    import app.services.kg_merge as kg_merge

    lifecycle = repo._runtime.knowledge_lifecycle
    nb = _make_kg(repo)
    lifecycle.rebuild_unified_kg(nb.id)
    with repo._runtime.database.write() as db:
        db.execute("UPDATE unified_kg_state SET cluster_code_version='' "
                   "WHERE notebook_id=?", (nb.id,))

    calls = _spy_stream(repo, monkeypatch)
    lifecycle.rebuild_unified_kg(nb.id)                  # force=False, inputs unchanged
    assert calls["n"] == 0                               # it was the skip path
    assert _code_version(repo, nb.id)[1] == kg_merge.cluster_code_version(
        lifecycle.settings)

    stamps = []
    original = repo._runtime.unified_kg.stamp_cluster_code_version
    monkeypatch.setattr(
        repo._runtime.unified_kg, "stamp_cluster_code_version",
        lambda *a, **k: stamps.append(a) or original(*a, **k))
    lifecycle.rebuild_unified_kg(nb.id)
    assert calls["n"] == 0 and stamps == []              # settled skip: zero writes


def test_a_failing_skip_path_stamp_is_fail_open(repo, monkeypatch):
    """The stamp is a derived write on a path that used to be write-free and
    always returned: if it fails (here: SQLite's busy timeout behind a long
    write) the skip still returns the cached count, emits
    ``cluster_code_version_stamp_failed`` and leaves the column for the next
    skip to fill. Mutation anchor: drop the try/except in ``_settle_skip_path``
    and ``rebuild_unified_kg`` raises."""
    import sqlite3

    import app.services.kg_merge as kg_merge

    lifecycle = repo._runtime.knowledge_lifecycle
    nb = _make_kg(repo)
    count = lifecycle.rebuild_unified_kg(nb.id)
    with repo._runtime.database.write() as db:
        db.execute("UPDATE unified_kg_state SET cluster_code_version='' "
                   "WHERE notebook_id=?", (nb.id,))

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    stamp = repo._runtime.unified_kg.stamp_cluster_code_version
    monkeypatch.setattr(repo._runtime.unified_kg, "stamp_cluster_code_version", locked)
    events = []
    original_emit = lifecycle.event_log.emit
    monkeypatch.setattr(lifecycle.event_log, "emit",
                        lambda event, *a, **k: events.append(dict(event))
                        or original_emit(event, *a, **k))
    calls = _spy_stream(repo, monkeypatch)

    assert lifecycle.rebuild_unified_kg(nb.id) == count       # still the skip
    assert calls["n"] == 0
    assert _code_version(repo, nb.id)[1] == ""
    failures = [e for e in events if e.get("kind") == "cluster_code_version_stamp_failed"]
    assert failures == [{"kind": "cluster_code_version_stamp_failed",
                         "notebook_id": nb.id, "error_class": "OperationalError"}]
    # The exception text stays out of the persisted event (it can carry
    # internal paths or database diagnostics); only the class name is kept.
    assert "database is locked" not in repr(failures)

    monkeypatch.setattr(repo._runtime.unified_kg, "stamp_cluster_code_version",
                        stamp)                                  # lock released
    lifecycle.rebuild_unified_kg(nb.id)
    assert _code_version(repo, nb.id)[1] == kg_merge.cluster_code_version(
        lifecycle.settings)


def test_algo_bump_reads_as_mismatch(repo, monkeypatch):
    """Bumping CLUSTER_ALGO_VERSION leaves kg_mutation_seq and dirty alone, so
    only the code component can tell a reader the last merge is stale: the
    stored value no longer equals the running code's. The next force=False
    rebuild re-merges (the input version moved) and stores the new one."""
    import app.services.kg_merge as kg_merge

    lifecycle = repo._runtime.knowledge_lifecycle
    nb = _make_kg(repo)
    lifecycle.rebuild_unified_kg(nb.id)
    before = _code_version(repo, nb.id)[1]
    assert before == kg_merge.cluster_code_version(lifecycle.settings)

    monkeypatch.setattr(kg_merge, "CLUSTER_ALGO_VERSION",
                        kg_merge.CLUSTER_ALGO_VERSION + 1)
    current = kg_merge.cluster_code_version(lifecycle.settings)
    assert _code_version(repo, nb.id)[1] != current      # reads as a mismatch
    calls = _spy_stream(repo, monkeypatch)
    lifecycle.rebuild_unified_kg(nb.id)
    assert calls["n"] >= 1                               # a real re-merge, not a skip
    assert _code_version(repo, nb.id)[1] == current
