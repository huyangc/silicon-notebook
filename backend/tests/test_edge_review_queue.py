# backend/tests/test_edge_review_queue.py
"""Integration tests for edge review queue and feedback loop.
Uses a real SQLiteRepository (in-memory / tmp_path) with FakeEmbedder.
Synthetic graph with 4 nodes and 3 edges.
"""
import threading
from contextlib import contextmanager

import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL",  f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM",       "16")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _seed_graph(repo) -> str:
    """Insert 4 KG nodes + 3 typed edges. Returns notebook_id."""
    nb = repo.create_notebook(NotebookCreate(name="test-nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "C1", "object_type": "Claim",
         "payload": {"name": "Claim Alpha"}, "evidence": []},
        {"local_id": "C2", "object_type": "Concept",
         "payload": {"name": "Concept Beta"}, "evidence": []},
        {"local_id": "F1", "object_type": "Formula",
         "payload": {"name": "Formula Gamma"}, "evidence": []},
        {"local_id": "P1", "object_type": "Procedure",
         "payload": {"name": "Procedure Delta"}, "evidence": []},
    ], [
        # Valid typed edge with evidence
        {"source_local_id": "C1", "target_local_id": "C2",
         "edge_type": "defines",
         "evidence": [{"file": "f1", "char_start": 0, "char_end": 10,
                       "line_start": 1, "line_end": 1, "quote": "alpha defines beta"}]},
        # Valid typed edge, NO evidence
        {"source_local_id": "F1", "target_local_id": "P1",
         "edge_type": "used_in", "evidence": []},
        # Type-violating edge (Claim→Procedure is not a valid pair for used_in)
        {"source_local_id": "C1", "target_local_id": "P1",
         "edge_type": "used_in", "evidence": []},
    ])
    return nb.id


# ── Schema migration ──────────────────────────────────────────────────────────

def test_review_queue_returns_list(repo):
    nb_id = _seed_graph(repo)
    q = repo.review_queue(nb_id)
    assert isinstance(q, list)
    assert len(q) >= 1


def test_review_queue_items_have_required_fields(repo):
    nb_id = _seed_graph(repo)
    q = repo.review_queue(nb_id)
    for item in q:
        assert "rel_id" in item
        assert "trust_score" in item
        assert "edge_centrality" in item
        assert "review_priority" in item
        assert "review_status" in item
        assert "edge_type" in item
        assert 0.0 <= item["trust_score"] <= 1.0
        assert item["review_priority"] >= 0.0


def test_review_queue_type_violating_edge_lower_trust(repo):
    """The type-violating edge (Claim→Procedure used_in) should have lower
    trust_score than the correctly-typed, evidenced edge (Claim→Concept defines)."""
    nb_id = _seed_graph(repo)
    q = repo.review_queue(nb_id)
    # Find the defines edge (valid + evidence) and the invalid used_in edge
    defines_item = next((i for i in q if i["edge_type"] == "defines"), None)
    # Both used_in edges — pick the one from Claim (type-violating)
    invalid_used_in = next(
        (i for i in q if i["edge_type"] == "used_in" and
         i.get("source_type") == "Claim"), None)
    if defines_item and invalid_used_in:
        assert defines_item["trust_score"] > invalid_used_in["trust_score"]


def test_review_queue_sorted_by_priority_desc(repo):
    """Items are sorted by review_priority descending (highest-risk first)."""
    nb_id = _seed_graph(repo)
    q = repo.review_queue(nb_id)
    priorities = [item["review_priority"] for item in q]
    assert priorities == sorted(priorities, reverse=True)


def test_review_queue_excludes_rejected(repo):
    """After marking an edge rejected, it must not appear in the review queue."""
    nb_id = _seed_graph(repo)
    q_before = repo.review_queue(nb_id)
    assert q_before, "need at least one edge"
    rel_id = q_before[0]["rel_id"]
    repo.set_edge_review(nb_id, rel_id, "rejected")
    q_after = repo.review_queue(nb_id)
    assert all(item["rel_id"] != rel_id for item in q_after)


# ── set_edge_review ───────────────────────────────────────────────────────────

def test_set_edge_review_persists_status(repo):
    nb_id = _seed_graph(repo)
    q = repo.review_queue(nb_id)
    rel_id = q[0]["rel_id"]
    repo.set_edge_review(nb_id, rel_id, "verified")
    with repo._connect() as db:
        row = db.execute(
            "SELECT review_status FROM knowledge_relations WHERE id=?", (rel_id,)
        ).fetchone()
    assert row["review_status"] == "verified"


def test_set_edge_review_invalid_status_raises(repo):
    nb_id = _seed_graph(repo)
    q = repo.review_queue(nb_id)
    rel_id = q[0]["rel_id"]
    with pytest.raises(ValueError, match="review_status"):
        repo.set_edge_review(nb_id, rel_id, "bogus_status")


# ── R3 T-A3 review (P1-2 / S1 / F1) ──────────────────────────────────────────

def test_governance_store_update_edge_review_returns_prev_status(repo):
    """update_edge_review's return value contract (P1-2): it must hand back the
    PREVIOUS review_status, not None — a fresh relation starts 'pending'."""
    nb_id = _seed_graph(repo)
    rel_id = repo.review_queue(nb_id)[0]["rel_id"]
    with repo._write() as db:
        prev = repo._runtime.governance.update_edge_review(db, nb_id, rel_id, "verified")
    assert prev == "pending"
    with repo._write() as db:
        prev2 = repo._runtime.governance.update_edge_review(db, nb_id, rel_id, "rejected")
    assert prev2 == "verified"


def test_governance_store_update_edge_review_missing_relation_raises_keyerror(repo):
    """rowcount==0 (no matching id/notebook) must still raise KeyError, not
    silently return None — regression guard for the SELECT-then-UPDATE
    rewrite of the old cur.rowcount check."""
    nb_id = _seed_graph(repo)
    with repo._write() as db:
        with pytest.raises(KeyError):
            repo._runtime.governance.update_edge_review(
                db, nb_id, "rel-does-not-exist", "verified"
            )


# ── R2 P2 (codex #638 R2): bump-in-tx fix — concurrent/failure edge cases ────

def test_concurrent_opposite_flips_never_leave_the_memo_disagreeing_with_the_db(
    repo, monkeypatch
):
    """P2-a reproduction + fix confirmation. Two writers flip the SAME
    relation to opposite non-rejected statuses. This forces the exact
    interleave codex described: writer A's call is paused RIGHT AFTER its
    first write-transaction commits (lock already released, so this is a
    real inter-transaction window, not a hold-the-lock stall), writer B's
    ENTIRE set_edge_review call is allowed to run to completion while A is
    paused, and only THEN is A allowed to resume and reach its own memo
    carry/invalidate call.

    Before the R2 fix (bump + seq-readback living in a SEPARATE, later
    transaction/connection from the UPDATE) this interleave lets A resume
    with a seq value that has since been advanced by B's own bump, carry
    successfully against B's already-correct memo entry, and overwrite it
    with A's status at a seq that still looks perfectly valid — the DB ends
    up with B's write but the memo ends up with A's status. After the fix
    (bump + readback inside the SAME transaction as the UPDATE) A's own
    new_seq is fixed the moment its transaction commits, before B can even
    start — B's own carry then targets a memo tag A has not written yet and
    is correctly dropped, and A's later carry finds nothing to overwrite.
    The invariant this test pins: whatever the memo ends up holding for this
    relation, it never disagrees with the DB while still looking like a
    valid (non-dropped) entry.

    Mutation self-check (see the task report): reverting set_edge_review to
    the old three-transaction shape makes this test fail exactly as
    described above; restoring the fix makes it pass again.
    """
    nb_id = _seed_graph(repo)
    rel_id = repo.review_queue_page(nb_id)["items"][0]["rel_id"]  # warm the memo

    database = repo._runtime.database
    original_write = database.write
    commit_count = {"n": 0}
    count_lock = threading.Lock()
    reached_pause = threading.Event()
    release_pause = threading.Event()

    @contextmanager
    def controlled_write():
        with original_write() as db:
            yield db
        # `original_write`'s own __exit__ already committed and released the
        # write lock by this point — this is a genuine POST-commit,
        # lock-free window, not a stall while holding the lock.
        with count_lock:
            commit_count["n"] += 1
            first = commit_count["n"] == 1
        if first:
            reached_pause.set()
            assert release_pause.wait(timeout=5), "test deadlocked waiting to be released"

    monkeypatch.setattr(database, "write", controlled_write)

    outcome = {}

    def call_a():
        repo.set_edge_review(nb_id, rel_id, "verified")
        outcome["a_done"] = True

    def call_b():
        repo.set_edge_review(nb_id, rel_id, "pending")
        outcome["b_done"] = True

    thread_a = threading.Thread(target=call_a)
    thread_a.start()
    assert reached_pause.wait(timeout=5), "writer A never reached its post-commit pause"

    thread_b = threading.Thread(target=call_b)
    thread_b.start()
    thread_b.join(timeout=5)
    assert outcome.get("b_done"), "writer B's whole call must complete while A is paused"

    release_pause.set()
    thread_a.join(timeout=5)
    assert outcome.get("a_done"), "writer A must resume and complete after release"

    with repo._connect() as db:
        db_status = db.execute(
            "SELECT review_status FROM knowledge_relations WHERE id=?", (rel_id,)
        ).fetchone()["review_status"]
    assert db_status == "pending", "B's UPDATE committed last, so it is the DB's terminal value"

    memo = repo._runtime.review_queue_memo
    entry = memo._store.get(nb_id)
    if entry is not None:
        _seq, items, _total = entry
        memo_status = next(
            (i["review_status"] for i in items if i["rel_id"] == rel_id), None
        )
        if memo_status is not None:
            assert memo_status == db_status, (
                "memo carried a status that disagrees with the committed DB "
                "state under a seq tag that still looked valid — the R2 P2-a bug"
            )


def test_set_edge_review_rolls_back_atomically_when_the_dirty_bump_fails(repo, monkeypatch):
    """P2-b (first form), now unconstructable in its original shape: the
    dirty bump rides the SAME transaction as the review_status UPDATE, so a
    failure inside it can no longer land between an already-committed UPDATE
    and a bump that silently never ran — the whole transaction rolls back
    together. DB and memo end up exactly as they were before the call."""
    nb_id = _seed_graph(repo)
    page_before = repo.review_queue_page(nb_id)  # warm the memo
    rel_id = page_before["items"][0]["rel_id"]
    memo = repo._runtime.review_queue_memo
    seq_before = memo.cached_seq(nb_id)
    assert seq_before is not None

    def boom(connection, notebook_id):
        raise RuntimeError("injected dirty-bump failure")

    monkeypatch.setattr(repo._runtime.kg_mutations, "mark_unified_kg_dirty_in_tx", boom)

    with pytest.raises(RuntimeError, match="injected dirty-bump failure"):
        repo.set_edge_review(nb_id, rel_id, "verified")

    with repo._connect() as db:
        row = db.execute(
            "SELECT review_status FROM knowledge_relations WHERE id=?", (rel_id,)
        ).fetchone()
    assert row["review_status"] == "pending", "the UPDATE must roll back together with the bump"
    assert memo.cached_seq(nb_id) == seq_before, "memo must be untouched by the aborted write"


def test_set_edge_review_carry_failure_after_commit_leaves_a_stale_memo_that_recomputes_correctly(
    repo, monkeypatch
):
    """P2-b (second form): the UPDATE+bump transaction has ALREADY committed
    by the time carry() would run. Injecting a failure right at that boundary
    (immediately after commit, immediately before the memo bookkeeping) shows
    the safe direction the fix guarantees: the memo is left exactly as it was
    BEFORE this call — strictly stale, since its tag can no longer match the
    seq this call's transaction already committed — never wrongly agreeing
    with the new DB state. The next read misses on that stale tag and
    cold-recomputes a result that matches the DB."""
    nb_id = _seed_graph(repo)
    page_before = repo.review_queue_page(nb_id)  # warm the memo
    rel_id = page_before["items"][0]["rel_id"]
    memo = repo._runtime.review_queue_memo
    seq_before = memo.cached_seq(nb_id)
    assert seq_before is not None
    real_carry = memo.carry

    def failing_carry(*args, **kwargs):
        raise RuntimeError("injected carry failure")

    monkeypatch.setattr(memo, "carry", failing_carry)

    with pytest.raises(RuntimeError, match="injected carry failure"):
        repo.set_edge_review(nb_id, rel_id, "verified")  # pending -> verified (would carry)

    with repo._connect() as db:
        row = db.execute(
            "SELECT review_status FROM knowledge_relations WHERE id=?", (rel_id,)
        ).fetchone()
    assert row["review_status"] == "verified", "the UPDATE+bump already committed before carry ran"
    assert memo.cached_seq(nb_id) == seq_before, "memo must be untouched — stale, not wrong"

    monkeypatch.setattr(memo, "carry", real_carry)
    page_after = repo.review_queue_page(nb_id)  # stale tag -> cold recompute
    assert {i["rel_id"]: i["review_status"] for i in page_after["items"]}[rel_id] == "verified"


def test_set_edge_review_verified_flip_carries_the_memo_without_cold_recompute(
    repo, monkeypatch
):
    """T-A3 v4 (codex #638 R1): a pure pending->verified flip must carry-
    forward the ranking memo — items AND total live in ONE entry now — via a
    cheap retag, NOT a cold recompute. The retagged total must be exactly the
    value the earlier cold scan produced, not a fresh count (counter
    assertion on the real ``_rank_review_queue`` cold path)."""
    nb_id = _seed_graph(repo)
    page_before = repo.review_queue_page(nb_id)  # warm the memo (cold scan #1)
    rel_id = page_before["items"][0]["rel_id"]
    calls = _count_cold_rankings(repo._runtime.knowledge_governance, monkeypatch)

    repo.set_edge_review(nb_id, rel_id, "verified")  # pending -> verified

    assert calls["n"] == 0, "verified<->pending 翻转不得触发冷排名/冷计数"
    page_after = repo.review_queue_page(nb_id)
    assert calls["n"] == 0, "served warm (carried), not a fresh cold scan"
    assert page_after["total"] == page_before["total"]
    assert {i["rel_id"]: i["review_status"] for i in page_after["items"]}[rel_id] == "verified"


def test_set_edge_review_reject_invalidates_the_memo_and_recomputes_total(repo):
    """T-A3 v4: a transition touching 'rejected' must invalidate (not carry)
    the ranking memo — queue membership actually changed, so the next read's
    total must reflect exactly one fewer non-rejected edge."""
    nb_id = _seed_graph(repo)
    page_before = repo.review_queue_page(nb_id)
    rel_id = page_before["items"][0]["rel_id"]
    memo = repo._runtime.review_queue_memo
    assert memo.cached_seq(nb_id) is not None

    repo.set_edge_review(nb_id, rel_id, "rejected")  # pending -> rejected

    assert memo.cached_seq(nb_id) is None  # popped, not stale-retagged
    page_after = repo.review_queue_page(nb_id)
    assert page_after["total"] == page_before["total"] - 1
    assert all(i["rel_id"] != rel_id for i in page_after["items"])


def test_set_edge_review_unreject_invalidates_the_memo_and_recomputes_total(repo):
    """T-A3 v4: rejected -> pending (undoing a rejection) is exactly as
    membership-changing as the forward direction and must also invalidate,
    never carry — the edge re-enters the (review_status != 'rejected') set,
    so total climbs back up by exactly one."""
    nb_id = _seed_graph(repo)
    rel_id = repo.review_queue(nb_id)[0]["rel_id"]
    repo.set_edge_review(nb_id, rel_id, "rejected")
    page_after_reject = repo.review_queue_page(nb_id)  # warm again post-reject
    memo = repo._runtime.review_queue_memo
    assert memo.cached_seq(nb_id) is not None

    repo.set_edge_review(nb_id, rel_id, "pending")  # rejected -> pending (un-reject)

    assert memo.cached_seq(nb_id) is None
    page_after_unreject = repo.review_queue_page(nb_id)
    assert page_after_unreject["total"] == page_after_reject["total"] + 1
    assert rel_id in {i["rel_id"] for i in page_after_unreject["items"]}


def test_review_queue_page_missing_notebook_raises_keyerror(repo):
    """S1: review_queue_page must guard notebook existence the SAME way
    review_queue already does (symmetry) — a direct/service-level caller
    (bypassing the API route's own dependency) must see the same failure."""
    with pytest.raises(KeyError):
        repo.review_queue_page("does-not-exist")


# ── R3 T-A2: 排名 memo 的端到端(审核循环不再每次重排) ─────────────────────

def _count_cold_rankings(governance, monkeypatch):
    """给 ``_rank_review_queue``(冷路径:全量取数 + 打分 + betweenness)套计数器,
    委托真实实现,所以命中/未命中之外的一切行为都保持原样。"""
    calls = {"n": 0}
    original = governance._rank_review_queue

    def spy(notebook_id, limit):
        calls["n"] += 1
        return original(notebook_id, limit)

    monkeypatch.setattr(governance, "_rank_review_queue", spy)
    return calls


def test_review_queue_verified_flip_carries_the_ranking_without_recomputing(
    repo, monkeypatch
):
    """T-A2 主判据:一次 pending->verified 的审核判定之后,同进程的下一次取队列
    **不得**再跑一遍冷排名——而且续下来的那份必须与真正重算的结果逐位相同。"""
    nb_id = _seed_graph(repo)
    before = repo.review_queue(nb_id)                      # 冷算一次,暖起来
    rel_id = before[0]["rel_id"]
    calls = _count_cold_rankings(repo._runtime.knowledge_governance, monkeypatch)

    repo.set_edge_review(nb_id, rel_id, "verified")
    carried = repo.review_queue(nb_id)

    assert calls["n"] == 0, "verified<->pending 翻转不得触发冷排名"
    assert [i["rel_id"] for i in carried] == [i["rel_id"] for i in before]
    assert [i["review_priority"] for i in carried] == [
        i["review_priority"] for i in before
    ]
    assert {i["rel_id"]: i["review_status"] for i in carried}[rel_id] == "verified"
    # 续来的这份 == 真重算的那份(carry 的正确性,不只是「没重算」)。
    repo._runtime.review_queue_memo.invalidate(nb_id)
    assert repo.review_queue(nb_id) == carried
    assert calls["n"] == 1


def test_review_queue_reject_invalidates_the_ranking(repo):
    """任一侧涉及 'rejected' 的迁移会改变集合与拓扑——排名 memo 必须被丢掉,
    而不是像 verified 翻转那样续标签。"""
    nb_id = _seed_graph(repo)
    rel_id = repo.review_queue(nb_id)[0]["rel_id"]
    memo = repo._runtime.review_queue_memo
    assert memo.cached_seq(nb_id) is not None

    repo.set_edge_review(nb_id, rel_id, "rejected")

    assert memo.cached_seq(nb_id) is None
    assert rel_id not in {i["rel_id"] for i in repo.review_queue(nb_id)}


def test_review_queue_unreject_invalidates_the_ranking(repo):
    """撤销拒绝(rejected -> pending)把边**加回**集合,与正向一样改变拓扑与
    corroboration 分组;carry 在这里是错的。"""
    nb_id = _seed_graph(repo)
    rel_id = repo.review_queue(nb_id)[0]["rel_id"]
    repo.set_edge_review(nb_id, rel_id, "rejected")
    repo.review_queue(nb_id)                               # 重新暖起来(不含该边)
    memo = repo._runtime.review_queue_memo
    assert memo.cached_seq(nb_id) is not None

    repo.set_edge_review(nb_id, rel_id, "pending")

    assert memo.cached_seq(nb_id) is None
    assert rel_id in {i["rel_id"] for i in repo.review_queue(nb_id)}


def test_two_uncarried_bumps_force_a_recompute(repo, monkeypatch):
    """跨进程模拟:别的进程连 bump 两次 seq(本进程收不到任何 carry),于是本地
    条目的标签落后两个版本。此后哪怕来一次 verified 翻转,它的 carry 也会因为
    ``expected_seq`` 不符而整条丢弃——绝不把陈旧内容续成新版本。"""
    nb_id = _seed_graph(repo)
    rel_id = repo.review_queue(nb_id)[0]["rel_id"]
    memo = repo._runtime.review_queue_memo
    warm_seq = memo.cached_seq(nb_id)

    repo._mark_unified_kg_dirty(nb_id)                     # 另一个进程的两次 KG 变更
    repo._mark_unified_kg_dirty(nb_id)
    assert memo.cached_seq(nb_id) == warm_seq              # 本地标签没动

    calls = _count_cold_rankings(repo._runtime.knowledge_governance, monkeypatch)
    repo.set_edge_review(nb_id, rel_id, "verified")
    assert memo.cached_seq(nb_id) is None, "seq 不符必须丢弃,不得续标签"
    repo.review_queue(nb_id)
    assert calls["n"] == 1


def test_review_queue_bypasses_the_memo_outside_the_cached_depth(repo, monkeypatch):
    """``limit < 0``(「掐掉尾巴」的切片)与 ``limit > M``(比 memo 存的还深)直通
    冷路径,语义 = 现状;区间内的 limit 仍然走 memo。"""
    from app.services.review_queue_memo import REVIEW_QUEUE_MEMO_ITEMS

    nb_id = _seed_graph(repo)
    repo.review_queue(nb_id)                               # 暖起来
    calls = _count_cold_rankings(repo._runtime.knowledge_governance, monkeypatch)

    repo.review_queue(nb_id, limit=-1)
    repo.review_queue(nb_id, limit=REVIEW_QUEUE_MEMO_ITEMS + 1)
    assert calls["n"] == 2

    repo.review_queue(nb_id, limit=REVIEW_QUEUE_MEMO_ITEMS)
    repo.review_queue(nb_id, limit=0)
    assert calls["n"] == 2


def test_review_queue_result_mutation_does_not_reach_the_memo(repo):
    nb_id = _seed_graph(repo)
    handed_out = repo.review_queue(nb_id)
    expected = [dict(item) for item in handed_out]
    handed_out[0]["review_status"] = "MUTATED"
    handed_out[0]["review_priority"] = -999.0
    handed_out.pop()

    assert repo.review_queue(nb_id) == expected


def test_review_queue_missing_notebook_raises_keyerror(repo):
    """``get_notebook`` 仍在 memo 之前:一个不存在的 notebook 必须照旧抛
    ``KeyError``,不能因为「先查 memo」而变成一次静默的空队列。"""
    with pytest.raises(KeyError):
        repo.review_queue("does-not-exist")


def test_add_relations_facade_path_invalidates_the_review_queue_ranking(repo):
    """T-A2 的同一处豁口:``add_relations`` 不 bump ``kg_mutation_seq``,排名 memo
    与计数 memo 一样必须在那里被显式失效。"""
    nb_id = _seed_graph(repo)
    repo.review_queue(nb_id)
    memo = repo._runtime.review_queue_memo
    assert memo.cached_seq(nb_id) is not None

    repo.add_relations(nb_id, "", [])

    assert memo.cached_seq(nb_id) is None


def test_delete_notebook_kg_invalidates_the_review_queue_ranking(repo):
    """``delete_notebook_kg`` 掉 ``unified_kg_state`` 整行,seq 因此**归零重爬**
    ——不是单调前进,而是别名:重抽会让 seq 爬回它离开时的值,配上完全不同的图。
    该方法已经为同一个理由显式失效计数 memo;排名 memo 必须并排跟上。"""
    nb_id = _seed_graph(repo)
    repo.review_queue(nb_id)
    memo = repo._runtime.review_queue_memo
    assert memo.cached_seq(nb_id) is not None

    repo.delete_notebook_kg(nb_id)

    assert memo.cached_seq(nb_id) is None
    assert repo.review_queue(nb_id) == []


# ── codex #638 R5 P1:store_kg 的 bump 与图行同事务 ────────────────────────


def _graph_seq(repo, nb_id: str) -> int:
    """``unified_kg_state.kg_mutation_seq`` 的当前值(行缺失记 0)。"""
    with repo._connect() as db:
        row = db.execute(
            "SELECT kg_mutation_seq FROM unified_kg_state WHERE notebook_id=?",
            (nb_id,),
        ).fetchone()
    return int(row[0]) if row is not None else 0


_ONE_MORE_EDGE_OBJECTS = [
    {"local_id": "N1", "object_type": "Claim",
     "payload": {"name": "Late Claim"}, "evidence": []},
    {"local_id": "N2", "object_type": "Concept",
     "payload": {"name": "Late Concept"}, "evidence": []},
]
_ONE_MORE_EDGE_RELATIONS = [
    {"source_local_id": "N1", "target_local_id": "N2",
     "edge_type": "defines", "evidence": []},
]


def test_store_kg_bump_is_visible_to_the_review_queue_while_it_still_embeds(
    repo, monkeypatch
):
    """codex #638 R5 P1(a):``store_kg`` 提交图行之后要跑 embedding(生产上分钟级),
    修法前 bump 排在 embedding **之后**——整个 embedding 期间「行已提交、seq 未动」,
    暖着的 ``ReviewQueueMemo`` 靠 seq 闸挡不住,持续端出陈旧的 items 与 total。

    这里在 embedding 回调里当场重演一次并发读:那一刻图行已经落库,所以一个正确的
    实现必须已经把 seq 推过 memo 的标签,读到的队列必须包含刚写进去的那条边。

    变异锚点:把 ``_mark_unified_kg_dirty_in_tx`` 挪回 ``with self._write()`` 之外
    (即恢复 embedding 之后的 post-tx bump),下面两条断言都会红——seq 原地不动,
    队列端出的还是暖那一刻的旧集合。"""
    nb_id = _seed_graph(repo)
    warm_rel_ids = {item["rel_id"] for item in repo.review_queue(nb_id)}
    memo = repo._runtime.review_queue_memo
    warm_seq = memo.cached_seq(nb_id)
    assert warm_seq is not None
    assert warm_seq == _graph_seq(repo, nb_id)

    observed: dict = {}

    def _observe_mid_embed(notebook_id, items, progress=None, commit_every=None):
        # The graph rows are committed by now; read the queue exactly as a
        # concurrent request would — through the same warm memo.
        observed["seq"] = _graph_seq(repo, nb_id)
        observed["rel_ids"] = {i["rel_id"] for i in repo.review_queue(nb_id)}

    monkeypatch.setattr(
        repo._runtime.source_embedding, "embed_objects_batch", _observe_mid_embed
    )

    repo.store_kg(
        nb_id, None,
        [dict(o) for o in _ONE_MORE_EDGE_OBJECTS],
        [dict(r) for r in _ONE_MORE_EDGE_RELATIONS],
    )

    assert observed["seq"] > warm_seq, (
        "the graph rows are already committed at this point, so their seq bump "
        "must have committed with them — not be waiting on the embeddings"
    )
    assert len(observed["rel_ids"]) == len(warm_rel_ids) + 1, (
        "a read landing during the embedding pass must see the just-committed "
        "edge, not the ranking the memo was warmed with"
    )
    assert observed["rel_ids"] > warm_rel_ids


def test_store_kg_embedding_failure_still_leaves_the_seq_advanced(
    repo, monkeypatch
):
    """codex #638 R5 P1(b):非替换路径上 embedding 抛异常会把异常抛回调用方——
    修法前 bump 排在它之后,于是**整个被跳过**:图行永久留在库里而 seq 一动不动,
    暖着的 memo 从此无限期端出陈旧结果,直到某次不相关的 KG 写把 seq 顶开。

    修法后行与 seq 已经一起提交,embedding 失败只影响向量:seq 已前进,memo 的
    标签因此失配,下一次读冷算出与 DB 一致的结果。"""
    nb_id = _seed_graph(repo)
    warm_rel_ids = {item["rel_id"] for item in repo.review_queue(nb_id)}
    memo = repo._runtime.review_queue_memo
    warm_seq = memo.cached_seq(nb_id)
    assert warm_seq is not None

    def _boom(notebook_id, items, progress=None, commit_every=None):
        raise RuntimeError("embedder down")

    monkeypatch.setattr(
        repo._runtime.source_embedding, "embed_objects_batch", _boom
    )

    with pytest.raises(RuntimeError):
        repo.store_kg(
            nb_id, None,
            [dict(o) for o in _ONE_MORE_EDGE_OBJECTS],
            [dict(r) for r in _ONE_MORE_EDGE_RELATIONS],
        )

    assert _graph_seq(repo, nb_id) > warm_seq, (
        "an embedding failure must not swallow the bump for rows that are "
        "already durable"
    )
    # store_kg raised before its cache-eviction tail, so nothing explicitly
    # cleared the memo — the seq gate alone has to catch this.
    assert memo.cached_seq(nb_id) == warm_seq
    live_rel_ids = {item["rel_id"] for item in repo.review_queue(nb_id)}
    assert len(live_rel_ids) == len(warm_rel_ids) + 1
    assert memo.cached_seq(nb_id) == _graph_seq(repo, nb_id)


def test_add_relations_facade_path_resets_the_cached_total(repo):
    """F1 / T-A3 v4: RepositoryFacade.add_relations is a raw-insert path that
    bypasses store_kg's kg_mutation_seq bump. It must explicitly invalidate
    the ranking memo (items AND total, v4 — one entry, not the old separate
    counts_cache memo) itself so a fixture that warms it, then seeds via this
    path, then reads again never sees a stale total."""
    nb_id = _seed_graph(repo)
    repo.review_queue_page(nb_id)  # warm the memo
    memo = repo._runtime.review_queue_memo
    assert memo.cached_seq(nb_id) is not None

    repo.add_relations(nb_id, "", [])  # no-op insert, but still the facade path

    assert memo.cached_seq(nb_id) is None  # explicitly invalidated


# ── R3 T-A3 v4: items/total 同版本一致性(codex #638 R1)────────────────────

def test_review_queue_page_hit_does_not_recompute_the_ranking_or_total(repo, monkeypatch):
    """memo 命中时 total 不重算:同一 KG 版本下多次 ``review_queue_page`` 只跑
    一次冷排名/冷计数(计数器断言),且两次读到的 items/total 逐位相同。"""
    nb_id = _seed_graph(repo)
    calls = _count_cold_rankings(repo._runtime.knowledge_governance, monkeypatch)

    first = repo.review_queue_page(nb_id)
    second = repo.review_queue_page(nb_id)

    assert calls["n"] == 1
    assert first == second


def test_review_queue_page_direct_bypass_path_computes_the_true_total(repo):
    """直通路径(``limit`` 超出 memo 深度,或为负)与 memo 路径必须在同一个
    seq 上算出相同的 ``total``——都来自 ``_rank_review_queue`` 同一次扫描,
    并且都必须等于对 ``knowledge_relations`` 直接做 ``COUNT(*)`` 得到的真值。"""
    from app.services.review_queue_memo import REVIEW_QUEUE_MEMO_ITEMS

    nb_id = _seed_graph(repo)
    memo_page = repo.review_queue_page(nb_id, limit=REVIEW_QUEUE_MEMO_ITEMS)
    direct_page_over = repo.review_queue_page(nb_id, limit=REVIEW_QUEUE_MEMO_ITEMS + 1)
    direct_page_negative = repo.review_queue_page(nb_id, limit=-1)

    with repo._connect() as db:
        row = db.execute(
            "SELECT COUNT(*) AS c FROM knowledge_relations "
            "WHERE notebook_id=? AND review_status != 'rejected'",
            (nb_id,),
        ).fetchone()
    true_total = int(row["c"])

    assert memo_page["total"] == true_total
    assert direct_page_over["total"] == true_total
    assert direct_page_negative["total"] == true_total


def test_review_queue_page_total_tracks_the_true_count_across_interleaved_rejects(repo):
    """items/total 同版本一致性,以一串连续的 reject 决定模拟多版本推进
    (每做一次决定就立刻重新读一次 page):``total`` 必须始终等于「这次读到的
    items 所属那个 KG 版本」里非 rejected 的真实关系数,而不是任何更早或更晚
    版本的计数——不允许 items 已经反映某次 reject、total 却还没反映(或反过来)
    这种跨版本自相矛盾。"""
    nb_id = _seed_graph(repo)
    page = repo.review_queue_page(nb_id)
    all_rel_ids = [item["rel_id"] for item in page["items"]]
    assert len(all_rel_ids) >= 2, "seed graph 需要至少两条边才能测多次 reject 交错"

    for rel_id in all_rel_ids:
        repo.set_edge_review(nb_id, rel_id, "rejected")
        page = repo.review_queue_page(nb_id)
        with repo._connect() as db:
            row = db.execute(
                "SELECT COUNT(*) AS c FROM knowledge_relations "
                "WHERE notebook_id=? AND review_status != 'rejected'",
                (nb_id,),
            ).fetchone()
        assert page["total"] == int(row["c"])
        assert rel_id not in {i["rel_id"] for i in page["items"]}


def test_review_queue_page_stays_internally_consistent_when_a_reject_races_the_cold_scan(
    repo, monkeypatch
):
    """并发交错(注入模拟):另一个请求在本次冷扫描的「取数」阶段(seq 已经点读
    完毕之后)插队做了一次 reject 决定并提交。读序契约保证这次冷算的 items 与
    total 仍然出自SAME一次扫描——要么两者都还没反映那次 reject,要么两者都已
    反映,绝不会一半新一半旧(比如 total 已经扣掉了那条边、items 里却还留着它)。
    这次结果的标签必然落后于世界的最新 seq,所以下一次读会因为 seq 不等而重新
    冷算,吐出真正干净的新版本。"""
    nb_id = _seed_graph(repo)
    page0 = repo.review_queue_page(nb_id)  # warm once
    rel_id_to_reject = page0["items"][0]["rel_id"]
    governance = repo._runtime.knowledge_governance
    original_rank = governance._rank_review_queue
    fired = {"done": False}

    def racing_rank(notebook_id, limit):
        if not fired["done"]:
            fired["done"] = True
            # 模拟另一个并发请求在本次冷算读数据之前完成了一次 reject 并提交。
            repo.set_edge_review(notebook_id, rel_id_to_reject, "rejected")
        return original_rank(notebook_id, limit)

    monkeypatch.setattr(governance, "_rank_review_queue", racing_rank)
    repo._runtime.review_queue_memo.invalidate(nb_id)  # force the next read cold

    page1 = repo.review_queue_page(nb_id)

    with repo._connect() as db:
        row = db.execute(
            "SELECT review_status FROM knowledge_relations WHERE id=?",
            (rel_id_to_reject,),
        ).fetchone()
    assert row["review_status"] == "rejected"  # the injected race really landed

    # 内部一致性:racing_rank 在真正读数据之前就提交了 reject,所以这次扫描看到
    # 的的确是 reject 之后的干净状态——items 与 total 必须彼此吻合。
    contains = rel_id_to_reject in {i["rel_id"] for i in page1["items"]}
    assert not contains
    assert page1["total"] == page0["total"] - 1

    # 下一次读必须是全新、自洽的一次冷算(seq 已经因 reject 前进,memo 被打上的
    # 标签落后于世界最新版本),而不是续用被打断的那一份——结果应当保持稳定。
    page2 = repo.review_queue_page(nb_id)
    assert page2["total"] == page1["total"]
    assert page2["items"] == page1["items"]


# ── Feedback loop: rejected edges demoted in graph ───────────────────────────
# C3 (hotpath cleanup): this section used to test the feedback loop through
# `SqliteRepository._rx_graph`, the single-notebook reasoning-graph loader.
# `_rx_graph` had zero production callers — reasoning's follow_chain always
# goes through `_federated_rx_graph` (base+active merge; a solo personal notebook
# with no base participants federates to just itself, so it subsumes the
# single-notebook case) — so `_rx_graph` was deleted as dead code. The cache-
# invalidation-on-warm-graph and rejected/verified-edge-visibility assertions
# below were ported to `_federated_rx_graph` (see "Feedback loop × federated
# graph" section, which already covered rejection + warm-cache invalidation
# — `test_rejected_personal_edge_excluded_from_federated_graph` implicitly
# proves a not-yet-rejected edge is visible in a warm graph too). The one
# genuinely distinct assertion — multihop_subgraph traversal skipping a
# rejected edge — is ported here as
# `test_verify_chain_edges_skips_rejected_federated` so no coverage is lost.

def _rx_edge_rel_ids(G) -> set:
    """Collect all rel_ids present in a PyDiGraph returned by _federated_rx_graph."""
    rel_ids = set()
    for src_idx in range(G.num_nodes()):
        for tgt_idx in G.successor_indices(src_idx):
            payload = G.get_edge_data(src_idx, tgt_idx)
            if isinstance(payload, dict):
                rel_ids.add(payload.get("rel_id", ""))
    return rel_ids


def test_verify_chain_edges_skips_rejected_federated(repo):
    """A subgraph traversal (as used by verify_chain_edges/follow_chain) on the
    federated reasoning graph (the live path — see module comment above) where
    a rejected edge has been excluded should not include that edge at all."""
    nb_id = _seed_graph(repo)
    q = repo.review_queue(nb_id)
    # Reject the first edge
    rel_id = q[0]["rel_id"]
    repo.set_edge_review(nb_id, rel_id, "rejected")
    # Traverse the graph — rejected edge should not appear in any subgraph
    from app.services.kg.graph_reason import multihop_subgraph, DEFAULT_REASONING_EDGES
    G, idx_to_oid, oid_to_idx = repo._federated_rx_graph(nb_id)
    all_oids = list(oid_to_idx.keys())
    sub = multihop_subgraph(G, oid_to_idx, idx_to_oid,
                            seed_ids=all_oids[:1],
                            edge_types=DEFAULT_REASONING_EDGES,
                            max_depth=3, max_fan_out=10)
    sub_rel_ids = {e["rel_id"] for _, e, _ in sub if e and "rel_id" in e}
    assert rel_id not in sub_rel_ids


# ── Feedback loop × federated graph (Track D integration) ────────────────────
# ask(mode=graph) reasons over _federated_rx_graph (base + personal merged) —
# the only reasoning-graph loader in the repo (see module comment above) — so
# the rejected-edge demotion is proven directly against it.

def _seed_federated(repo):
    """Base notebook (marked base) + personal notebook, one edge each.

    Returns (base_id, pers_id). _federated_rx_graph(pers_id) merges both.
    """
    base_nb = repo.create_notebook(NotebookCreate(name="base-nb"))
    repo.mark_notebook_base(base_nb.id)
    repo.store_kg(base_nb.id, None, [
        {"local_id": "B1", "object_type": "Formula",
         "payload": {"name": "Base Formula"}, "evidence": []},
        {"local_id": "B2", "object_type": "Claim",
         "payload": {"name": "Base Claim"}, "evidence": []},
    ], [
        {"source_local_id": "B1", "target_local_id": "B2",
         "edge_type": "derived_from",
         "evidence": [{"file": "f1", "char_start": 0, "char_end": 10,
                       "line_start": 1, "line_end": 1,
                       "quote": "base formula derives base claim"}]},
    ])
    pers_nb = repo.create_notebook(NotebookCreate(name="personal-nb"))
    repo.store_kg(pers_nb.id, None, [
        {"local_id": "P1", "object_type": "Concept",
         "payload": {"name": "Personal Concept"}, "evidence": []},
        {"local_id": "P2", "object_type": "Claim",
         "payload": {"name": "Personal Claim"}, "evidence": []},
    ], [
        {"source_local_id": "P1", "target_local_id": "P2",
         "edge_type": "supports",
         "evidence": [{"file": "f2", "char_start": 0, "char_end": 10,
                       "line_start": 1, "line_end": 1,
                       "quote": "personal concept supports personal claim"}]},
    ])
    repo.replace_notebook_bases(pers_nb.id, [base_nb.id], "user-local")
    return base_nb.id, pers_nb.id


def test_rejected_personal_edge_excluded_from_federated_graph(repo):
    """Rejecting a PERSONAL edge must drop it from a warm federated graph —
    without the review filter in _federated_rx_graph's loader, the rejected
    edge would keep flowing into ask(mode=graph) reasoning."""
    base_id, pers_id = _seed_federated(repo)
    pers_rel_id = repo.review_queue(pers_id)[0]["rel_id"]

    # Warm the federated cache with the edge still active.
    G_warm, _, _ = repo._federated_rx_graph(pers_id)
    assert pers_rel_id in _rx_edge_rel_ids(G_warm)

    repo.set_edge_review(pers_id, pers_rel_id, "rejected")

    G_fresh, _, _ = repo._federated_rx_graph(pers_id)
    assert pers_rel_id not in _rx_edge_rel_ids(G_fresh), (
        "rejected personal edge still present in the federated reasoning graph")


def test_rejected_base_edge_excluded_from_federated_graph_for_personal_active(repo):
    """Rejecting a BASE-notebook edge must drop it from the PERSONAL notebook's
    warm federated graph (cross-participant invalidation + loader filter)."""
    base_id, pers_id = _seed_federated(repo)
    base_rel_id = repo.review_queue(base_id)[0]["rel_id"]

    G_warm, _, _ = repo._federated_rx_graph(pers_id)
    assert base_rel_id in _rx_edge_rel_ids(G_warm)

    # Review verdict lands on the BASE notebook; the federated cache key is
    # "{pers}:fed_rxgraph" — both the evict-all-fed eviction and the
    # per-participant version key must cover this.
    repo.set_edge_review(base_id, base_rel_id, "rejected")

    G_fresh, _, _ = repo._federated_rx_graph(pers_id)
    assert base_rel_id not in _rx_edge_rel_ids(G_fresh), (
        "rejected base edge still present in the personal federated graph")


def test_federated_version_key_covers_review_flip_without_eviction(repo, monkeypatch):
    """Pin the SOUND federated version key (per-status counts per participant).

    set_edge_review's explicit _invalidate_unified_cache (which evicts all
    *:fed_rxgraph) is belt-and-braces; here the eviction is no-op'd so the
    test fails unless the federated version tuple ALONE detects the
    verified→rejected flip — (COUNT, MAX created_at) cannot, since a status
    UPDATE changes neither.
    """
    base_id, pers_id = _seed_federated(repo)
    pers_rel_id = repo.review_queue(pers_id)[0]["rel_id"]
    repo.set_edge_review(pers_id, pers_rel_id, "verified")

    G_warm, _, _ = repo._federated_rx_graph(pers_id)
    assert pers_rel_id in _rx_edge_rel_ids(G_warm)

    # Disable explicit eviction: only the version key can force a rebuild now.
    monkeypatch.setattr(repo, "_invalidate_unified_cache", lambda nb_id: None)
    repo.set_edge_review(pers_id, pers_rel_id, "rejected")

    G_fresh, _, _ = repo._federated_rx_graph(pers_id)
    assert pers_rel_id not in _rx_edge_rel_ids(G_fresh), (
        "stale federated graph served after verified→rejected flip — "
        "version key does not cover review_status")


# ── API endpoints (Track E — thin wrappers over repo methods) ────────────────

@pytest.fixture
def client(repo, monkeypatch):
    """TestClient with the knowledge router repository overridden to the fixture repo."""
    from fastapi.testclient import TestClient
    import app.api.knowledge_routes as routes_mod
    from app.main import app
    monkeypatch.setattr(routes_mod, "repository", lambda: repo)
    return TestClient(app)


def test_api_edge_review_queue_returns_items(client, repo):
    nb_id = _seed_graph(repo)
    resp = client.get(f"/api/notebooks/{nb_id}/edge-review-queue")
    assert resp.status_code == 200
    body = resp.json()
    # response_model=EdgeReviewQueueResponse: {"items": [...], "total": n} (R3 T-A3),
    # not a bare list.
    assert isinstance(body, dict) and {"items", "total"} <= set(body)
    items = body["items"]
    assert isinstance(items, list) and items
    # response_model item shape keeps the curation fields
    assert {"rel_id", "trust_score", "edge_centrality", "review_priority",
            "review_status"} <= set(items[0])
    # Highest-risk first (priority desc)
    priorities = [i["review_priority"] for i in items]
    assert priorities == sorted(priorities, reverse=True)
    # total is the true queue size, independent of any `limit` truncation —
    # here the unlimited seed graph is small enough that it equals len(items).
    assert body["total"] == len(items)


def test_api_edge_review_queue_missing_notebook_404(client):
    resp = client.get("/api/notebooks/does-not-exist/edge-review-queue")
    assert resp.status_code == 404


def test_api_review_relation_round_trip(client, repo):
    nb_id = _seed_graph(repo)
    before = client.get(f"/api/notebooks/{nb_id}/edge-review-queue").json()
    rel_id = repo.review_queue(nb_id)[0]["rel_id"]
    resp = client.post(
        f"/api/notebooks/{nb_id}/relations/{rel_id}/review",
        json={"status": "rejected"})
    assert resp.status_code == 200
    assert resp.json() == {"rel_id": rel_id, "review_status": "rejected"}
    # Rejected edge drops out of the queue surfaced by the API
    after = client.get(f"/api/notebooks/{nb_id}/edge-review-queue").json()
    assert all(i["rel_id"] != rel_id for i in after["items"])
    # ...and the true total drops by exactly one rejected edge (not just the page).
    assert after["total"] == before["total"] - 1


def test_api_review_relation_bad_status_400(client, repo):
    nb_id = _seed_graph(repo)
    rel_id = repo.review_queue(nb_id)[0]["rel_id"]
    resp = client.post(
        f"/api/notebooks/{nb_id}/relations/{rel_id}/review",
        json={"status": "nonsense"})
    assert resp.status_code == 400


def test_api_review_relation_missing_rel_404(client, repo):
    nb_id = _seed_graph(repo)
    resp = client.post(
        f"/api/notebooks/{nb_id}/relations/rel-missing/review",
        json={"status": "verified"})
    assert resp.status_code == 404
