"""Batch 3·W1 PR-3 Phase B — PostgreSQL-only additions: phase 3's form-two
(ctid) EXPLAIN shape, and phase 4's real per-notebook scale-build claim
contention + on-disk artifact-root cleanup. SQLite-reachable behavior (the
table registry, chain sequencing, resume/idempotency, the runner's stall
logic) is already pinned by ``tests/test_notebook_delete_rows_and_files.py``
— this file only covers what can plausibly diverge by BACKEND.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.core.config import Settings


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_notebook_delete_rows_files"),
]

NOW = "2026-09-01T00:00:00+00:00"


@pytest.fixture
def postgres_settings_with_storage(postgres_scope, tmp_path):
    return Settings(
        database_url=postgres_scope.url,
        storage_dir=str(tmp_path),
        postgres_pool_min_size=1,
        postgres_pool_max_size=4,
        postgres_pool_acquire_timeout_seconds=2,
        postgres_statement_timeout_seconds=5,
        postgres_lock_timeout_seconds=2,
    )


@pytest.fixture
def postgres_repository(postgres_settings_with_storage):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings_with_storage)
    try:
        yield repository
    finally:
        repository.close()


def _insert_user(db, user_id: str) -> None:
    db.execute(
        "INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (user_id, f"{user_id}@x", user_id.upper(), "user", "active", user_id, NOW, NOW),
    )


def _insert_notebook(db, nid: str, owner: str, status: str = "ready") -> None:
    db.execute(
        "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at)"
        " VALUES (%s,%s,%s,%s,%s,%s)",
        (nid, f"NB-{nid}", owner, status, NOW, NOW),
    )


# ---------------------------------------------------------------------------
# 1. Form-two EXPLAIN shape (G3: "形二专项...EXPLAIN 必须是 Tid Scan +
#    内层 Index Scan")
# ---------------------------------------------------------------------------


def test_form_two_delete_uses_tid_scan_with_an_inner_index_scan(postgres_repository):
    """``enable_seqscan=off`` forces the choice between the ctid path and a
    full scan — same idiom ``test_knowledge_store_conformance.py``/
    ``test_search_conformance.py`` already use for this class of EXPLAIN
    assertion (a handful of test rows would otherwise let the planner
    legitimately prefer Seq Scan on cost alone, which proves nothing about
    the plan SHAPE this form exists to guarantee at real scale)."""
    repo = postgres_repository
    owner = "u-pg-form2-explain"
    nb = "nb-pg-form2-explain"
    with repo._write() as db:
        _insert_user(db, owner)
        _insert_notebook(db, nb, owner)
        db.execute(
            "INSERT INTO community_members (canonical_id,notebook_id,level,"
            "community_id,canonical_name,centrality) VALUES (%s,%s,%s,%s,%s,%s)",
            ("c1", nb, 0, "comm1", "C1", 0.0),
        )
        db.execute("SET LOCAL enable_seqscan=off")
        plan = db.execute(
            "EXPLAIN (FORMAT TEXT) DELETE FROM community_members WHERE ctid = "
            "ANY(ARRAY(SELECT ctid FROM community_members WHERE notebook_id=%s "
            "LIMIT 500))",
            (nb,),
        ).fetchall()
    text = "\n".join(row["QUERY PLAN"] for row in plan)
    assert "Tid Scan" in text, text
    assert "Index" in text, text


def test_knowledge_object_sources_form_two_uses_tid_scan(postgres_repository):
    """闭包外补删、无 PK 的表同样要过这道 EXPLAIN 门（design §7 G3）。"""
    repo = postgres_repository
    owner = "u-pg-form2-kos"
    nb = "nb-pg-form2-kos"
    with repo._write() as db:
        _insert_user(db, owner)
        _insert_notebook(db, nb, owner)
        db.execute("SET LOCAL enable_seqscan=off")
        plan = db.execute(
            "EXPLAIN (FORMAT TEXT) DELETE FROM knowledge_object_sources WHERE "
            "ctid = ANY(ARRAY(SELECT ctid FROM knowledge_object_sources "
            "WHERE notebook_id=%s LIMIT 500))",
            (nb,),
        ).fetchall()
    text = "\n".join(row["QUERY PLAN"] for row in plan)
    assert "Tid Scan" in text, text


# ---------------------------------------------------------------------------
# 2. Phase 3 + 4 end to end on real PostgreSQL, including a form-two table
# ---------------------------------------------------------------------------


def test_phase3_clears_a_form_two_table_on_real_postgres(postgres_repository):
    repo = postgres_repository
    owner = "u-pg-form2-e2e"
    nb = "nb-pg-form2-e2e"
    with repo._write() as db:
        _insert_user(db, owner)
        _insert_notebook(db, nb, owner)
        for i in range(5):
            db.execute(
                "INSERT INTO community_members (canonical_id,notebook_id,"
                "level,community_id,canonical_name,centrality) VALUES "
                "(%s,%s,%s,%s,%s,%s)",
                (f"c{i}", nb, 0, "comm1", f"C{i}", 0.0),
            )

    rt = repo._runtime
    job = rt.notebook_delete_jobs.request(nb, owner)
    rt.notebook_delete.run(job["id"])

    with repo._connect() as db:
        remaining = db.execute(
            "SELECT COUNT(*) AS c FROM community_members WHERE notebook_id=%s",
            (nb,),
        ).fetchone()["c"]
        notebooks_left = db.execute(
            "SELECT COUNT(*) AS c FROM notebooks WHERE id=%s", (nb,)
        ).fetchone()["c"]
    assert remaining == 0
    assert notebooks_left == 0


def test_medium_library_covers_a_class_b_class_and_closure_external_on_postgres(
    postgres_repository,
):
    """G3's "造一个覆盖 A 类、B 类、形二 22 张、闭包外 6 张的中等库" on a real
    PostgreSQL connection — one source with elements+embeddings, a memory
    item with all three children, a knowhow table with a full column/row/
    cell/change chain, an ask job with a trace step and an ``answers`` row
    (protected — see the ``answers`` archive-dependency note in
    ``notebook_delete_tables``), a conversation (closure-external), and a
    form-two closure-external row (``knowledge_object_sources``, no PK)."""
    repo = postgres_repository
    owner = "u-pg-medium"
    nb = "nb-pg-medium"
    full_question = "如何降低噪声？这是历史已完成提问的尾部检索词"
    with repo._write() as db:
        _insert_user(db, owner)
        _insert_notebook(db, nb, owner)
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,file_path,source_type,"
            "status,parse_status,uploaded_by,created_at,updated_at) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("s1", nb, "S", "", "pdf", "ready", "parsed", owner, NOW, NOW),
        )
        db.execute(
            "INSERT INTO source_elements (id,source_id,element_type,"
            "location_label,text,metadata,created_at,ordinal) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s)",
            ("s1-el", "s1", "paragraph", "p1", "hello", "{}", NOW, 0),
        )
        db.execute(
            "INSERT INTO element_embeddings (element_id,source_id,"
            "notebook_id,vector,created_at) VALUES (%s,%s,%s,%s,%s)",
            ("s1-el", "s1", nb, "[]", NOW),
        )
        db.execute(
            "INSERT INTO knowledge_object_sources (object_id,source_id,"
            "notebook_id) VALUES (%s,%s,%s)",
            ("ko1", "s1", nb),
        )
        db.execute(
            "INSERT INTO memory_items (id,notebook_id,created_by,origin,"
            "status,promotion_state,title,content_md,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("m1", nb, owner, "ask_answer", "candidate", "none", "T", "C",
             NOW, NOW),
        )
        db.execute(
            "INSERT INTO memory_embeddings (memory_id,model,dimension,"
            "vector,updated_at) VALUES (%s,%s,%s,%s,%s)",
            ("m1", "m", 1, "[]", NOW),
        )
        db.execute(
            "INSERT INTO knowhow_tables (id,notebook_id,title,description,"
            "mutation_seq,created_by,created_at,updated_at) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s)",
            ("kt1", nb, "KT", "", 0, owner, NOW, NOW),
        )
        db.execute(
            "INSERT INTO knowhow_columns (id,table_id,name,role,position) "
            "VALUES (%s,%s,%s,%s,%s)",
            ("kc1", "kt1", "Col", "value", 0),
        )
        db.execute(
            "INSERT INTO knowhow_rows (id,table_id,position,"
            "projection_status,created_at,updated_at) VALUES "
            "(%s,%s,%s,%s,%s,%s)",
            ("kr1", "kt1", 0, "none", NOW, NOW),
        )
        db.execute(
            "INSERT INTO knowhow_cells (id,row_id,column_id,content_md,"
            "updated_at) VALUES (%s,%s,%s,%s,%s)",
            ("kx1", "kr1", "kc1", "v", NOW),
        )
        db.execute(
            "INSERT INTO answers (id,notebook_id,question,payload,"
            "created_at) VALUES (%s,%s,%s,%s,%s)",
            ("answer-1", nb, full_question, "{}", NOW),
        )
        db.execute(
            "INSERT INTO ask_jobs (id,notebook_id,conversation_id,"
            "created_by,mode,question,status,answer_id,created_at,"
            "updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            ("ask-1", nb, "", owner, "chunk", "如何降低噪声？", "completed",
             "answer-1", NOW, NOW),
        )
        db.execute(
            "INSERT INTO ask_trace_steps (job_id,seq,step_json,created_at) "
            "VALUES (%s,%s,%s,%s)",
            ("ask-1", 0, "{}", NOW),
        )
        db.execute(
            "INSERT INTO conversations (id,notebook_id,title,created_by,"
            "created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s)",
            ("cv1", nb, "C", owner, NOW, NOW),
        )

    rt = repo._runtime
    job = rt.notebook_delete_jobs.request(nb, owner)
    # P2-h: 相位 5(finalize)在这个中型夹具上的耗时打点——只作可见性用,不作断言
    # 阈值(生产尺度的实测正式挪到 T-0,见 design 文档「实现期勘误」;这个中型
    # 夹具的对象/行数远小于生产库,数字本身不能代表生产耗时,只用来在 CI 输出里
    # 留一个可追踪的基线信号)。
    original_finalize = rt.notebook_delete._phase_finalize
    phase5_duration_seconds: list[float] = []

    def _timed_finalize(job_id: str, notebook_id: str, lease_token: str) -> None:
        started = time.perf_counter()
        try:
            original_finalize(job_id, notebook_id, lease_token)
        finally:
            phase5_duration_seconds.append(time.perf_counter() - started)

    rt.notebook_delete._phase_finalize = _timed_finalize
    try:
        rt.notebook_delete.run(job["id"])
    finally:
        rt.notebook_delete._phase_finalize = original_finalize
    assert phase5_duration_seconds, "相位 5 应当被打点恰好一次(未走残渣收尾路径)"
    print(
        f"[P2-h] PG 中型夹具相位 5(finalize)耗时 = "
        f"{phase5_duration_seconds[0] * 1000:.2f}ms(仅可见性打点,非生产尺度基线,"
        f"生产尺度实测见 design 文档「实现期勘误」T-0 登记项)"
    )

    with repo._connect() as db:
        def count(sql, params):
            return db.execute(sql, params).fetchone()["c"]

        assert count("SELECT COUNT(*) AS c FROM notebooks WHERE id=%s", (nb,)) == 0
        assert count(
            "SELECT COUNT(*) AS c FROM source_elements WHERE source_id=%s", ("s1",),
        ) == 0
        assert count(
            "SELECT COUNT(*) AS c FROM element_embeddings WHERE notebook_id=%s", (nb,),
        ) == 0
        assert count(
            "SELECT COUNT(*) AS c FROM knowledge_object_sources WHERE notebook_id=%s",
            (nb,),
        ) == 0
        assert count(
            "SELECT COUNT(*) AS c FROM memory_items WHERE notebook_id=%s", (nb,),
        ) == 0
        assert count(
            "SELECT COUNT(*) AS c FROM memory_embeddings WHERE memory_id=%s", ("m1",),
        ) == 0
        assert count(
            "SELECT COUNT(*) AS c FROM knowhow_tables WHERE notebook_id=%s", (nb,),
        ) == 0
        assert count(
            "SELECT COUNT(*) AS c FROM knowhow_cells WHERE row_id=%s", ("kr1",),
        ) == 0
        assert count(
            "SELECT COUNT(*) AS c FROM ask_trace_steps WHERE job_id=%s", ("ask-1",),
        ) == 0
        assert count(
            "SELECT COUNT(*) AS c FROM conversations WHERE notebook_id=%s", (nb,),
        ) == 0
        # The archive-equivalence assertion this medium library exists to
        # pin: `answers` must have survived through phase 3 for phase 5's
        # ask-projection LEFT JOIN to read its full question text.
        archived = db.execute(
            "SELECT question FROM retained_user_activity WHERE record_id=%s",
            ("ask-1",),
        ).fetchone()
        assert archived["question"] == full_question


# ---------------------------------------------------------------------------
# 3. Phase 4: real on-disk artifact roots + .old/.tmp/.tmp-<token> siblings
# ---------------------------------------------------------------------------


def _make_artifact_tree(storage_dir: Path, nb: str) -> list[Path]:
    made = []
    for root in ("kg_index", "kg_viz", "kg_index_partitions"):
        base = storage_dir / root
        for suffix in ("", ".old", ".tmp", ".tmp-abc123"):
            d = base / f"{nb}{suffix}"
            d.mkdir(parents=True)
            (d / "manifest.json").write_text("{}")
            made.append(d)
    return made


def test_phase4_removes_every_artifact_root_and_its_old_tmp_siblings(
    postgres_repository, postgres_settings_with_storage,
):
    repo = postgres_repository
    owner = "u-pg-artifacts"
    nb = "nb-pg-artifacts"
    with repo._write() as db:
        _insert_user(db, owner)
        _insert_notebook(db, nb, owner)

    storage_dir = Path(postgres_settings_with_storage.storage_dir)
    made = _make_artifact_tree(storage_dir, nb)
    # An UNRELATED notebook's artifact tree must survive untouched.
    other = _make_artifact_tree(storage_dir, "nb-pg-artifacts-other")

    rt = repo._runtime
    job = rt.notebook_delete_jobs.request(nb, owner)
    rt.notebook_delete.run(job["id"])

    for path in made:
        assert not path.exists(), path
    for path in other:
        assert path.exists(), path


# ---------------------------------------------------------------------------
# 4. Phase 4 defers (Busy semantics) when the scale-build claim is held
#    elsewhere, and resumes once it is released
# ---------------------------------------------------------------------------


def test_phase4_parks_waiting_when_scale_build_lock_is_held_elsewhere(
    postgres_repository, postgres_settings_with_storage,
):
    from app.repositories.postgres.database import PostgresDatabase
    from app.repositories.scale_build_lock import SCALE_BUILD_LOCK_UNAVAILABLE

    repo = postgres_repository
    owner = "u-pg-lock-busy"
    nb = "nb-pg-lock-busy"
    with repo._write() as db:
        _insert_user(db, owner)
        _insert_notebook(db, nb, owner)

    storage_dir = Path(postgres_settings_with_storage.storage_dir)
    made = _make_artifact_tree(storage_dir, nb)

    other_database = PostgresDatabase(
        postgres_settings_with_storage,
        Path(__file__).resolve().parents[3],
    )
    try:
        holder = other_database.try_scale_build_lock(nb)
        # P3 (code review): `holder is not object()` is a tautology --
        # `object()` constructs a fresh, never-equal-by-identity instance
        # every time, so that assertion could never fail. The real claim
        # this test needs is that acquisition actually succeeded (not
        # `None`/held elsewhere, not the exhausted-session-budget sentinel).
        assert holder is not None
        assert holder is not SCALE_BUILD_LOCK_UNAVAILABLE

        rt = repo._runtime
        job = rt.notebook_delete_jobs.request(nb, owner)
        rt.notebook_delete.run(job["id"])

        waiting = rt.notebook_delete_jobs.get(job["id"])
        # P2-c: phases 3-5's claim-unavailable case parks 'queued', not
        # 'waiting' -- 'waiting' is reserved for phase 2's quiesce alone.
        assert waiting["status"] == "queued"
        # Nothing on disk was touched while the claim was unavailable.
        for path in made:
            assert path.exists(), path

        holder.release()
    finally:
        other_database.close()

    # Once released, a resubmit (what the sweep would do) completes.
    rt = repo._runtime
    rt.notebook_delete.run(job["id"])
    with repo._connect() as db:
        remaining = db.execute(
            "SELECT COUNT(*) AS c FROM notebooks WHERE id=%s", (nb,)
        ).fetchone()["c"]
    assert remaining == 0
    for path in made:
        assert not path.exists(), path
