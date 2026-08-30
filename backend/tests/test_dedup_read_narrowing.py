# backend/tests/test_dedup_read_narrowing.py
"""R3 T-B1 (KG-3, audit P0) — `find_duplicates` 两趟取数的等价性与有界性护栏。

`find_duplicates` 原本用 `_knowledge_objects` 把整个 (notebook, object_type)
切片的全列 payload+evidence 一次性读进 Python，再做 seed 分块与打分。本文件的
主证据是把改造前的算法原样写成 oracle（复用仍然存在、未被本改动触碰的
`_knowledge_objects`，只把「两趟取数」这一段重新手抄一遍冻结住），逐用例对账
分组成员集合、similarity、排序；另加两条 SQL 文本护栏钉住 pass 2 的 SQLite
危险写法（沿用 `test_governance_read_narrowing.py` 的模式）。

沿用 `test_kg_empty_extraction_marker.py` 的两侧逐用例风格：每个场景一条独立
的最小夹具 + 一次「新实现 == oracle」断言，而不是一个大夹具堆所有场景。
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


# ── the pre-two-pass-split implementation, kept verbatim as an oracle ───────

def _old_find_duplicates(repo, notebook_id: str, object_type: str) -> list:
    """`find_duplicates` exactly as it read before the R3 T-B1 two-pass split:
    one full-column fetch via `_knowledge_objects` (payload+evidence for
    EVERY object of the type), a Python-side `status != 'deprecated'` filter,
    then the UNCHANGED seed/alias/similarity/sort pipeline.

    `_knowledge_objects` itself is untouched by T-B1 — other consumers still
    read through it — so this oracle calls the SAME production method; only
    the two-pass split under test is reimplemented here, frozen at the state
    it was in before this change.
    """
    from app.services.kg_merge import (
        build_acronym_alias_map, _seed_with_alias,
        seed_concept, seed_claim, seed_formula, seed_procedure,
    )
    seed_fn = {
        "concept": seed_concept, "claim": seed_claim,
        "formula": seed_formula, "procedure": seed_procedure,
    }.get(object_type, seed_concept)

    governance = repo._runtime.knowledge_governance
    with repo._connect() as db:
        objs = governance._knowledge_objects(db, notebook_id, object_type, statuses=None)
    objs = [o for o in objs if o.get("status") != "deprecated"]

    alias_map = build_acronym_alias_map(o["payload"].get("name", "") for o in objs)
    by_seed: dict = {}
    for o in objs:
        seed = _seed_with_alias(
            {"name": o["payload"].get("name", ""), "payload": o["payload"]},
            seed_fn, alias_map)
        if seed:
            by_seed.setdefault(seed, []).append(o)

    groups = []
    for members in by_seed.values():
        if len(members) < 2:
            continue
        best = 1.0
        if len(members) <= 25:
            best = 0.0
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    best = max(
                        best, governance._knowledge_similarity(members[i], members[j], {})
                    )
        groups.append({
            "object_type": object_type,
            "similarity": round(best, 3),
            "members": [governance._knowledge_ref(m, object_type) for m in members],
        })
    groups.sort(key=lambda g: (-len(g["members"]), -g["similarity"]))
    return groups


def _assert_matches_oracle(repo, notebook_id: str, object_type: str) -> list:
    """Run both implementations and assert full equivalence — object_type,
    similarity AND the exact member sequence (id/object_type/headline/status)
    of every group, in order. Returns the new implementation's groups for the
    caller's own extra assertions."""
    new_groups = repo.find_duplicates(notebook_id, object_type)
    old_groups = _old_find_duplicates(repo, notebook_id, object_type)
    assert len(new_groups) == len(old_groups)
    for new_g, old_g in zip(new_groups, old_groups):
        assert new_g.object_type == old_g["object_type"]
        assert new_g.similarity == old_g["similarity"]
        assert list(new_g.members) == list(old_g["members"])
    return new_groups


# ── scenario 1: same-seed multi-member block + a singleton ─────────────────

def test_matches_oracle_for_a_multi_member_block_and_a_singleton(repo):
    nb = repo.create_notebook(NotebookCreate(name="multi-member"))
    repo._test_insert_object(nb.id, "concept", {"name": "MOSFET"})
    repo._test_insert_object(nb.id, "concept", {"name": "mosfet"})
    repo._test_insert_object(nb.id, "concept", {"name": "MOSFET "})
    repo._test_insert_object(nb.id, "concept", {"name": "Cascode"})  # singleton

    groups = _assert_matches_oracle(repo, nb.id, "concept")
    assert len(groups) == 1
    assert len(groups[0].members) == 3


# ── scenario 2: deprecated filtering (pushed-down SQL predicate) ───────────

def test_matches_oracle_and_excludes_deprecated_members(repo):
    nb = repo.create_notebook(NotebookCreate(name="deprecated-filter"))
    a = repo._test_insert_object(nb.id, "concept", {"name": "Amplifier"})
    b = repo._test_insert_object(nb.id, "concept", {"name": "amplifier"})
    dead = repo._test_insert_object(nb.id, "concept", {"name": "AMPLIFIER"})
    with repo._write() as db:
        db.execute("UPDATE knowledge_objects SET status='deprecated' WHERE id=?", (dead,))

    groups = _assert_matches_oracle(repo, nb.id, "concept")
    assert len(groups) == 1
    ids = {m.id for m in groups[0].members}
    assert ids == {a, b}
    assert dead not in ids


def test_deprecated_filter_pushdown_catches_a_regression_if_removed(repo, monkeypatch):
    """Mutation self-check for the SQL pushdown: monkeypatch
    `duplicate_seed_rows` to the OLD behaviour (fetch every status, filter in
    Python is REMOVED so the caller sees the deprecated row too) and confirm
    the equivalence assertion above would have gone red. This does not touch
    the real predicate — it simulates "the pushdown regressed" by wrapping
    the real method and re-widening its result, which is externally
    indistinguishable from the SQL predicate itself having been dropped."""
    from app.repositories.sqlite.knowledge_store import KnowledgeStore

    nb = repo.create_notebook(NotebookCreate(name="deprecated-mutation"))
    a = repo._test_insert_object(nb.id, "concept", {"name": "Amplifier"})
    b = repo._test_insert_object(nb.id, "concept", {"name": "amplifier"})
    dead = repo._test_insert_object(nb.id, "concept", {"name": "AMPLIFIER"})
    with repo._write() as db:
        db.execute("UPDATE knowledge_objects SET status='deprecated' WHERE id=?", (dead,))

    governance = repo._runtime.knowledge_governance
    real = KnowledgeStore.duplicate_seed_rows

    def _without_status_pushdown(db, notebook_id, object_type):
        # Re-fetch the deprecated row directly (bypassing the pushdown) and
        # splice it back in — this is what "the predicate got deleted" looks
        # like from find_duplicates's point of view.
        rows = list(real(db, notebook_id, object_type))
        extra = db.execute(
            "SELECT id, status, json_extract(payload, '$.name') AS name "
            "FROM knowledge_objects WHERE id=? AND status='deprecated'",
            (dead,),
        ).fetchall()
        rows.extend({"id": r["id"], "status": r["status"], "name": r["name"]} for r in extra)
        return rows

    monkeypatch.setattr(governance.knowledge, "duplicate_seed_rows", _without_status_pushdown)
    with pytest.raises(AssertionError):
        _assert_matches_oracle(repo, nb.id, "concept")


# ── scenario 3: acronym alias redirection ───────────────────────────────────

def test_matches_oracle_for_acronym_alias_redirection(repo):
    nb = repo.create_notebook(NotebookCreate(name="acronym"))
    full = repo._test_insert_object(
        nb.id, "concept", {"name": "Compressed Sparse Attention (CSA)"}
    )
    bare = repo._test_insert_object(nb.id, "concept", {"name": "CSA"})
    repo._test_insert_object(nb.id, "concept", {"name": "Unrelated"})  # singleton

    groups = _assert_matches_oracle(repo, nb.id, "concept")
    assert len(groups) == 1
    assert {m.id for m in groups[0].members} == {full, bare}


# ── scenario 4: procedure steps-signature blocking ──────────────────────────

def test_matches_oracle_for_procedure_steps_signature(repo):
    nb = repo.create_notebook(NotebookCreate(name="procedure"))
    p1 = repo._test_insert_object(nb.id, "procedure", {
        "name": "Boot Sequence",
        "steps": [{"name": "Power On"}, {"name": "Load Kernel"}],
    })
    p2 = repo._test_insert_object(nb.id, "procedure", {
        "name": "boot sequence",
        "steps": [{"name": "power on"}, {"name": "load kernel"}],
    })
    # Same name, DIFFERENT steps signature — must stay a distinct (singleton)
    # seed, not merged onto p1/p2's block.
    repo._test_insert_object(nb.id, "procedure", {
        "name": "Boot Sequence",
        "steps": [{"name": "Different Step"}],
    })

    groups = _assert_matches_oracle(repo, nb.id, "procedure")
    assert len(groups) == 1
    assert {m.id for m in groups[0].members} == {p1, p2}


# ── scenario 5: similarity score itself (not just grouping) on a <=25 block ─

def test_similarity_score_matches_oracle_for_a_small_block(repo):
    nb = repo.create_notebook(NotebookCreate(name="similarity"))
    repo._test_insert_object(nb.id, "concept", {
        "name": "Amplifier",
        "definition": "A device that boosts electrical signals using transistors",
    })
    repo._test_insert_object(nb.id, "concept", {
        "name": "amplifier",
        "definition": "A component used to boost audio signals for output",
    })

    groups = _assert_matches_oracle(repo, nb.id, "concept")
    assert len(groups) == 1
    # Not a trivial 1.0 / 0.0 — proves the FULL (pass-2-backfilled) payload,
    # not just the pass-1 name, feeds the similarity score, same as before.
    assert 0.0 < groups[0].similarity < 1.0


# ── scenario 6: stable tie-break across equal (len, similarity) groups ─────

def test_stable_sort_tie_break_matches_oracle(repo):
    nb = repo.create_notebook(NotebookCreate(name="tie-break"))
    repo._test_insert_object(nb.id, "concept", {"name": "Twin One"})
    repo._test_insert_object(nb.id, "concept", {"name": "twin one"})
    repo._test_insert_object(nb.id, "concept", {"name": "Twin Two"})
    repo._test_insert_object(nb.id, "concept", {"name": "twin two"})

    groups = _assert_matches_oracle(repo, nb.id, "concept")
    assert len(groups) == 2
    assert groups[0].similarity == groups[1].similarity == 1.0
    assert len(groups[0].members) == len(groups[1].members) == 2
    # Insertion order (== pass-1 created_at,id order) breaks the tie: "Twin
    # One" was inserted first.
    assert {m.headline.lower() for m in groups[0].members} == {"twin one"}
    assert {m.headline.lower() for m in groups[1].members} == {"twin two"}


# ── scenario 7: pass-2 backfill must reorder to pass-1 order ───────────────

def test_pass2_backfill_is_reordered_to_pass1_order_regardless_of_sql_row_order(
    repo, monkeypatch,
):
    """Mutation self-check for the "must reassemble in pass-1 order" contract
    (design review B5b): force `duplicate_member_rows` to answer in REVERSED
    order and confirm the group's member sequence is unaffected — it must
    come from pass 1's (created_at, id) order, never from whatever order the
    `id IN (...)` backfill happens to return."""
    nb = repo.create_notebook(NotebookCreate(name="pass2-order"))
    ids = [
        repo._test_insert_object(nb.id, "concept", {"name": "Widget"}),
        repo._test_insert_object(nb.id, "concept", {"name": "widget"}),
        repo._test_insert_object(nb.id, "concept", {"name": "WIDGET"}),
    ]

    governance = repo._runtime.knowledge_governance
    original = governance.knowledge.duplicate_member_rows

    def _reversed_order(db, notebook_id, object_ids, **kw):
        rows = original(db, notebook_id, object_ids, **kw)
        return list(reversed(rows))

    monkeypatch.setattr(governance.knowledge, "duplicate_member_rows", _reversed_order)
    groups = repo.find_duplicates(nb.id, "concept")
    assert len(groups) == 1
    assert [m.id for m in groups[0].members] == ids


# ── SQL-text guards: pass 2's SQLite planner-safety contract ────────────────

def _captured_member_lookup_sql(object_ids) -> list:
    class _Cursor:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _SqlCapture:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=None):
            text = " ".join(str(sql).split())
            self.statements.append(text)
            return _Cursor([])

    from app.repositories.sqlite.knowledge_store import KnowledgeStore

    capture = _SqlCapture()
    KnowledgeStore.duplicate_member_rows(capture, "nb-shape", object_ids)
    return [sql for sql in capture.statements if "FROM knowledge_objects" in sql]


def test_sqlite_member_lookup_sql_keeps_no_notebook_predicate():
    """Same hazard, same recipe as `GovernanceStore.review_queue_rows`'s
    endpoint lookup (sqlite/governance_store.py:332-352): without `ANALYZE`
    (never run on production databases here), SQLite plans
    `notebook_id=? AND id IN (...)` as a per-batch notebook-wide scan instead
    of a primary-key seek — worse than the full-notebook read this narrowing
    exists to remove. `notebook_id` must be PROJECTED (so the caller can
    still filter on it), never predicated in the same statement as the id
    list."""
    for sql in _captured_member_lookup_sql(["ko-a", "ko-b"]):
        assert "notebook_id = ?" not in sql and "notebook_id=?" not in sql, sql
        assert "notebook_id" in sql.split("FROM")[0], sql
        assert " IN (" in sql, sql


def test_pg_member_lookup_sql_keeps_the_notebook_predicate():
    """The PostgreSQL twin does the OPPOSITE on purpose (see its docstring):
    PostgreSQL plans `notebook_id=%s AND id = ANY(...)` as a primary-key scan
    with `notebook_id` as a filter, so keeping the predicate there is safe
    and (unlike SQLite) does not need the bare-id-list workaround."""
    from app.repositories.postgres.knowledge_store import KnowledgeStore

    class _Cursor:
        def fetchall(self):
            return []

    class _SqlCapture:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=None):
            self.statements.append(" ".join(str(sql).split()))
            return _Cursor()

    capture = _SqlCapture()
    KnowledgeStore.duplicate_member_rows(capture, "nb-shape", ["ko-a", "ko-b"])
    lookups = [sql for sql in capture.statements if "FROM knowledge_objects" in sql]
    assert lookups
    for sql in lookups:
        assert "notebook_id=%s" in sql or "notebook_id = %s" in sql, sql
        assert "id = ANY(" in sql, sql
