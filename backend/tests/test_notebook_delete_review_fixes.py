"""Batch 3·W1 PR-3 Phase B — internal code-review follow-up fixes (P1-A
residual cleanup, P1-B claim spanning phases 3-5 + phase-3 verify_held,
P1-E failed-job backoff/ceiling, P2-b closure reconciliation). SQLite lane;
the PostgreSQL-only additions live in the existing
``tests/postgres/test_notebook_delete_jobization_pg.py`` and
``tests/postgres/test_notebook_delete_rows_and_files_pg.py``.
"""
from __future__ import annotations

import pytest

from pathlib import Path

from app.core.config import Settings
from app.repositories.ports import StaleLeaseFinalizeError
from app.services import notebook_delete as nd
from app.services import notebook_delete_tables as ndt
from app.services.sqlite_repository import SQLiteRepository


NOW = "2026-09-01T00:00:00"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings(_env_file=None))


def _seed_user_and_notebook(repo, notebook_id="nb1", owner="u1", status="ready"):
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO users (id,email,display_name,role,status,username,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (owner, f"{owner}@x", owner.upper(), "user", "active", owner, NOW, NOW),
        )
        db.execute(
            "INSERT INTO notebooks (id,name,created_by,status,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?)",
            (notebook_id, f"NB-{notebook_id}", owner, status, NOW, NOW),
        )


# ---------------------------------------------------------------------------
# P1-A: sweep driver-A's "job row present, notebooks row absent" special
# case — the residual-cleanup path.
# ---------------------------------------------------------------------------


def test_residual_cleanup_clears_closure_external_leftovers_without_archiving(repo):
    """作业行创建后、真正跑之前,notebooks 行被带外删除(模拟旧路径/DBA 手工
    删)——闭包外表(不带 FK,不会随之级联)必须仍被清理干净,但绝不能补写
    retained_user_activity(数据已不完整,补档比不补更糟)。"""
    _seed_user_and_notebook(repo)
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO community_members (canonical_id,notebook_id,level,"
            "community_id,canonical_name,centrality) VALUES (?,?,?,?,?,?)",
            ("c1", "nb1", 0, "comm1", "C1", 0.0),
        )

    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    with repo._runtime.database.write() as db:
        db.execute("DELETE FROM notebooks WHERE id='nb1'")  # out-of-band delete

    repo._runtime.notebook_delete.run(job["id"])

    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) c FROM community_members WHERE notebook_id='nb1'"
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) c FROM notebook_delete_jobs"
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) c FROM notebook_delete_files"
        ).fetchone()["c"] == 0
        # Never attempted to archive anything for a notebook whose source
        # rows are already gone.
        assert db.execute(
            "SELECT COUNT(*) c FROM retained_user_activity WHERE notebook_id='nb1'"
        ).fetchone()["c"] == 0


def test_residual_cleanup_resumes_after_a_crash_mid_phase(repo):
    """残渣收尾模式也要能崩溃续跑:模拟进程在相位 3 中途死掉后,sweep 重新
    submit 同一个 job_id,必须仍然收敛到零残留(不是从 mark 整个重来一遍,也
    不因为「以为还在正常 deleting」而卡死)。"""
    _seed_user_and_notebook(repo)
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO community_members (canonical_id,notebook_id,level,"
            "community_id,canonical_name,centrality) VALUES (?,?,?,?,?,?)",
            ("c1", "nb1", 0, "comm1", "C1", 0.0),
        )
    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    job_id = job["id"]
    with repo._runtime.database.write() as db:
        db.execute("DELETE FROM notebooks WHERE id='nb1'")
        # Simulate a worker that died mid phase-3, mid-quiesce-phase-marker,
        # with status still 'running' and a stale updated_at (driver A's
        # own staleness cutoff will pick this row back up).
        db.execute(
            "UPDATE notebook_delete_jobs SET status='running',"
            "updated_at='2000-01-01T00:00:00' WHERE id=?",
            (job_id,),
        )

    runner = repo._runtime.notebook_delete
    runner._sweep_seconds = 1
    submitted = runner.sweep_once()
    assert submitted == 1
    from app.services import background_jobs

    background_jobs._drain_maintenance_executors_for_tests(timeout=10.0)

    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) c FROM community_members WHERE notebook_id='nb1'"
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) c FROM notebook_delete_jobs"
        ).fetchone()["c"] == 0


# ---------------------------------------------------------------------------
# P1-D: the knowhow row/cell fanout is bounded via its own chain, run BEFORE
# knowhow_tables — checked against PHASE 3's OWN completeness (not phase 5's
# cascade safety net, which would clean up an orphaned knowhow_rows/cells
# either way and so cannot distinguish "phase 3 did it" from "cascade
# rescued a phase-3 gap").
# ---------------------------------------------------------------------------


def test_delete_knowhow_tables_page_never_rebuilds_the_unbounded_row_cell_fanout():
    """变异钉靶点（结构性）：``delete_knowhow_tables_page`` 的源码里不许再出现
    对 ``knowhow_rows``/``knowhow_cells``/``knowhow_cell_code`` 的引用——那正是
    P1-D 之前「一页 500 张表，展开全部 row/column id 拼进一条语句」的病灶所在。
    行为级测试(最终态收敛)在这里天然无区分度: SQLite 的外键 CASCADE 会在
    ``knowhow_tables`` 被删除时把 rows/cells 一并级联清空,即使显式的
    ``delete_knowhow_rows_page`` 链被整个删掉,最终计数也一样是零——所以这里
    直接检查源码,不测「结果对不对」，测「引入病灶的那段代码是否又被写回来」。
    本地 SQLite 编译的 SQLITE_MAX_VARIABLE_NUMBER 经探测高达 20 万+，不能指望
    在这台机器上真正撞见变量数上限来间接验证。"""
    import inspect

    from app.repositories.postgres.notebook_delete_job_store import (
        NotebookDeleteJobStore as PgStore,
    )
    from app.repositories.sqlite.notebook_delete_job_store import (
        NotebookDeleteJobStore as SqliteStore,
    )

    for store_cls in (PgStore, SqliteStore):
        source = inspect.getsource(store_cls.delete_knowhow_tables_page)
        # Check actual DELETE targets, not prose mentions of the sibling
        # method's name (which legitimately contains "knowhow_rows" as a
        # substring, e.g. in a cross-reference comment/docstring).
        for forbidden in (
            "FROM knowhow_rows", "FROM knowhow_cells", "FROM knowhow_cell_code",
        ):
            assert forbidden not in source, (
                f"{store_cls.__name__}.delete_knowhow_tables_page issues "
                f"{forbidden!r} -- the P1-D row/cell chain split has been "
                "reverted; that logic belongs in delete_knowhow_rows_page only"
            )
        assert hasattr(store_cls, "delete_knowhow_rows_page")


def test_every_chain_method_issues_an_explicit_delete_for_its_full_child_set():
    """P2-d（评审实测）：把 ``knowhow_cell_code``/``knowhow_milestones``/
    ``memory_revisions``/``indexing_pipeline_stages``/``indexing_pipeline_
    stage_sources`` 里任何一条 DELETE 语句删掉，最终态测试(逐表归零断言)
    仍然全绿——因为这五张表全部有到其父表的 ``ON DELETE CASCADE`` FK，父表
    被删时会把它们一并级联清空，行为级测试对「显式删了」和「反正会被级联
    捎带」没有区分度。这里直接检查每个 chain 方法的源码，确保这五条显式
    DELETE 语句都还在——结构性测试，不依赖最终状态。

    codex #659 round 9 P2 / round 10 P1：``delete_knowhow_tables_page``、
    ``delete_indexing_pipeline_stages_page``、``delete_knowhow_rows_page``
    都不再自己拼字面量 ``DELETE FROM knowhow_milestones``/``DELETE FROM
    indexing_pipeline_stage_sources``/``DELETE FROM knowhow_cell_code``——
    子表的删除都委托给通用的 ``_drain_children_by_parent_ids(child_table,
    ...)``，表名以字符串字面量的身份出现在传给它的实参里，不是
    ``f"FROM {table}"`` 这个具体形状。这几个方法改查字面量表名本身（同样
    能钉住「有人把某张子表从调用里删掉」这个回归），其余仍走原来那套
    ``FROM {table}`` 形状的检查。"""
    import inspect

    from app.repositories.postgres.notebook_delete_job_store import (
        NotebookDeleteJobStore as PgStore,
    )
    from app.repositories.sqlite.notebook_delete_job_store import (
        NotebookDeleteJobStore as SqliteStore,
    )

    expectations = {
        "delete_memory_items_page": ("memory_revisions",),
        "delete_indexing_pipeline_stages_page": ("indexing_pipeline_stages",),
    }
    delegated_to_drain_helper = {
        "delete_knowhow_tables_page": ("knowhow_milestones",),
        "delete_indexing_pipeline_stages_page": (
            "indexing_pipeline_stage_sources",
        ),
        "delete_knowhow_rows_page": ("knowhow_cell_code",),
    }
    for store_cls in (PgStore, SqliteStore):
        for method_name, required_tables in expectations.items():
            source = inspect.getsource(getattr(store_cls, method_name))
            for table in required_tables:
                assert f"FROM {table}" in source, (
                    f"{store_cls.__name__}.{method_name} no longer issues an "
                    f"explicit DELETE FROM {table} (P2-d)"
                )
        for method_name, required_tables in delegated_to_drain_helper.items():
            source = inspect.getsource(getattr(store_cls, method_name))
            for table in required_tables:
                assert f'"{table}"' in source, (
                    f"{store_cls.__name__}.{method_name} no longer drains "
                    f"{table} (P2-d / round 9-10)"
                )


def test_phase3_alone_clears_knowhow_rows_and_cells_before_phase5_ever_runs(repo):
    """变异钉靶点：调用 runner._phase_rows()（不推进到相位 4/5），断言
    knowhow_rows/knowhow_cells 已经清零——不依赖相位 5 那个「反正 cascade 兜底」
    的安全网（那个安全网对「_CHAINS 里删掉 knowhow_rows」这条回归完全没有
    区分度：即使相位 3 漏了，相位 5 的 DELETE FROM notebooks 级联也会把它们
    捎带删掉，最终态一样是零——所以必须在相位 3 自己完成的那一刻就检查）。"""
    _seed_user_and_notebook(repo)
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO knowhow_tables (id,notebook_id,title,description,"
            "mutation_seq,created_by,created_at,updated_at) VALUES "
            "(?,?,?,?,?,?,?,?)",
            ("kt1", "nb1", "KT", "", 0, "u1", NOW, NOW),
        )
        db.execute(
            "INSERT INTO knowhow_columns (id,table_id,name,role,position) "
            "VALUES (?,?,?,?,?)",
            ("kc1", "kt1", "Col", "value", 0),
        )
        db.execute(
            "INSERT INTO knowhow_rows (id,table_id,position,projection_status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?)",
            ("kr1", "kt1", 0, "none", NOW, NOW),
        )
        db.execute(
            "INSERT INTO knowhow_cells (id,row_id,column_id,content_md,"
            "updated_at) VALUES (?,?,?,?,?)",
            ("kx1", "kr1", "kc1", "v", NOW),
        )

    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    lease_token = repo._runtime.notebook_delete_jobs.mark_running(
        job["id"], stale_cutoff_seconds=300,
    )
    runner = repo._runtime.notebook_delete

    class _AlwaysHeldClaim:
        def verify_held(self):
            return True

        def release(self):
            pass

    finished = runner._phase_rows(
        job["id"], "nb1", lease_token, _AlwaysHeldClaim(), residual=False,
    )
    assert finished is True

    with repo._runtime.database.connect() as db:
        # Phase 3 alone, BEFORE phase 5 has run at all -- notebooks/
        # knowhow_tables rows are still both present at this point (phase 3
        # never touches notebooks; knowhow_tables IS a phase-3 direct-delete
        # table and should already be gone too).
        assert db.execute(
            "SELECT COUNT(*) c FROM notebooks WHERE id='nb1'"
        ).fetchone()["c"] == 1
        assert db.execute(
            "SELECT COUNT(*) c FROM knowhow_tables WHERE notebook_id='nb1'"
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) c FROM knowhow_rows WHERE table_id='kt1'"
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) c FROM knowhow_cells WHERE row_id='kr1'"
        ).fetchone()["c"] == 0


# ---------------------------------------------------------------------------
# P1-B: the independent claim spans phases 3-5, and phase 3 verifies it
# every batch (not just phase 4).
# ---------------------------------------------------------------------------


def test_phase3_stops_the_moment_the_claim_is_lost_mid_batch():
    """变异钉靶点:_batch_ok 必须在相位 3 的每一批里都复验 claim——这里用一个
    在第二次调用后失效的假 claim,断言循环在第二批之前就停手,而不是把整张
    表删完才发现丢了锁。"""
    from app.services.notebook_delete import NotebookDeleteJobRunner

    class _FlakyClaim:
        def __init__(self):
            self.calls = 0

        def verify_held(self):
            self.calls += 1
            return self.calls <= 1

        def release(self):
            pass

    class _FakeStore:
        def __init__(self):
            self.batches = 0

        def ownership_snapshot(self, job_id):
            return {"status": "running", "lease_token": "lease-1", "notebook_status": "deleting"}

        def delete_direct_page_form_one(self, table, id_column, filter_column, filter_value, cursor, limit):
            self.batches += 1
            if self.batches > 10:
                # Safety valve: a correct implementation stops after batch 1
                # (verify_held fails before batch 2 starts). Capping here
                # keeps a REGRESSED implementation's failure a clean,
                # bounded assertion failure ("ran too many batches") rather
                # than an unbounded hang under CI.
                return 0, None
            return 1, f"row-{self.batches}"

        def advance_phase(self, *a, **k):
            return True

        def mark_queued(self, *a, **k):
            return True

    runner = NotebookDeleteJobRunner.__new__(NotebookDeleteJobRunner)
    fake = _FakeStore()
    runner.delete_jobs = fake
    claim = _FlakyClaim()
    step = ndt.DirectTable("agent_notebook_profile", "one", pk_column="id")

    result = runner._run_direct_table(
        "job-1", "nb1", "lease-1", claim, "agent_notebook_profile", step, "",
        residual=False,
    )
    assert result is False
    assert fake.batches == 1  # stopped after the FIRST batch, not after draining
    assert claim.calls == 2  # verified before batch 1 (ok) and before batch 2 (lost)


# ---------------------------------------------------------------------------
# P1-E: failed jobs back off exponentially and stop after a ceiling.
# ---------------------------------------------------------------------------


def test_sweep_driver_b_stops_recreating_after_the_attempt_ceiling(repo):
    """变异钉靶点:把 attempts 推到上限(_MAX_DELETE_ATTEMPTS),sweep_once 的
    驱动 B 必须不再补建新作业行——去掉这条判断会让它无限重排。"""
    _seed_user_and_notebook(repo, status="deleting")
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO notebook_delete_jobs (id,notebook_id,status,phase,"
            "cursor_table,cursor_key,deleted_rows,lease_token,attempts,"
            "error_code,error_message,created_at,updated_at,finished_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ndj-old", "nb1", "failed", "mark", "", "", 0, "",
                nd._MAX_DELETE_ATTEMPTS, "notebook_delete_failed", "boom",
                NOW, NOW, NOW,
            ),
        )

    runner = repo._runtime.notebook_delete
    submitted = runner.sweep_once()
    assert submitted == 0
    with repo._runtime.database.connect() as db:
        # The old failed row is left alone (terminal diagnostic), not purged
        # or replaced -- purge only happens on a successful re-attempt.
        assert db.execute(
            "SELECT COUNT(*) c FROM notebook_delete_jobs WHERE id='ndj-old'"
        ).fetchone()["c"] == 1


def test_sweep_driver_b_respects_the_backoff_window_before_the_ceiling(repo):
    """还没到上限,但离上一次失败太近——不该立刻又补建一个。"""
    _seed_user_and_notebook(repo, status="deleting")
    from datetime import datetime, timedelta, timezone

    just_now = datetime.now(timezone.utc).astimezone().isoformat(timespec="microseconds")
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO notebook_delete_jobs (id,notebook_id,status,phase,"
            "cursor_table,cursor_key,deleted_rows,lease_token,attempts,"
            "error_code,error_message,created_at,updated_at,finished_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ndj-old", "nb1", "failed", "mark", "", "", 0, "", 1,
                "notebook_delete_failed", "boom", NOW, NOW, just_now,
            ),
        )

    runner = repo._runtime.notebook_delete
    submitted = runner.sweep_once()
    assert submitted == 0  # backoff window (60s for attempt 1) has not elapsed


def test_sweep_driver_b_purges_old_failed_rows_and_their_files_before_retrying(repo):
    """退避窗口已过、还没到上限——重排前必须先清掉旧 failed 行与它的
    notebook_delete_files 残留（P1-E：否则每次重试都在 side table 里再攒一份
    永远没人读的旧路径快照）。"""
    _seed_user_and_notebook(repo, status="deleting")
    old_finished = "2000-01-01T00:00:00+00:00"
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO notebook_delete_jobs (id,notebook_id,status,phase,"
            "cursor_table,cursor_key,deleted_rows,lease_token,attempts,"
            "error_code,error_message,created_at,updated_at,finished_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ndj-old", "nb1", "failed", "mark", "", "", 0, "", 1,
                "notebook_delete_failed", "boom", NOW, NOW, old_finished,
            ),
        )
        db.execute(
            "INSERT INTO notebook_delete_files (job_id,ordinal,file_path) "
            "VALUES ('ndj-old',0,'/tmp/orphan.pdf')",
        )

    runner = repo._runtime.notebook_delete
    submitted = runner.sweep_once()
    assert submitted == 1
    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) c FROM notebook_delete_jobs WHERE id='ndj-old'"
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) c FROM notebook_delete_files WHERE job_id='ndj-old'"
        ).fetchone()["c"] == 0
        new_row = db.execute(
            "SELECT attempts FROM notebook_delete_jobs WHERE notebook_id='nb1' "
            "AND id != 'ndj-old'"
        ).fetchone()
        assert new_row["attempts"] == 1  # carried forward, not reset to 0
    from app.services import background_jobs

    background_jobs._drain_maintenance_executors_for_tests(timeout=10.0)


def test_finish_increments_attempts(repo):
    _seed_user_and_notebook(repo)
    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    lease_token = repo._runtime.notebook_delete_jobs.mark_running(
        job["id"], stale_cutoff_seconds=300
    )
    settled = repo._runtime.notebook_delete_jobs.finish(
        job["id"], "failed", lease_token=lease_token, error_code="x", error_message="y",
    )
    assert settled
    row = repo._runtime.notebook_delete_jobs.get(job["id"])
    assert row["attempts"] == 1


def test_finish_is_a_noop_when_the_lease_no_longer_matches(repo):
    """P2-b (codex PR#659 round 1): a worker (A) that lost its lease to a
    second claim (B) must not be able to settle B's still-live row --
    finish() fenced out is a no-op, not a raise, and B's row is untouched."""
    _seed_user_and_notebook(repo)
    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    stale_lease = repo._runtime.notebook_delete_jobs.mark_running(
        job["id"], stale_cutoff_seconds=300
    )
    # Backdate updated_at so the sweep-driven stale-cutoff CAS branch of a
    # SECOND mark_running (simulating driver A resubmitting) can genuinely
    # steal it -- mark_running's own cutoff floors at 1 real second, so a
    # same-instant test can't trigger the steal without backdating.
    with repo._runtime.database.write() as db:
        db.execute(
            "UPDATE notebook_delete_jobs SET updated_at=? WHERE id=?",
            ("2020-01-01T00:00:00", job["id"]),
        )
    fresh_lease = repo._runtime.notebook_delete_jobs.mark_running(
        job["id"], stale_cutoff_seconds=300
    )
    assert fresh_lease is not None and fresh_lease != stale_lease

    # A's late exception tries to settle the job with its now-stale lease.
    settled = repo._runtime.notebook_delete_jobs.finish(
        job["id"], "failed", lease_token=stale_lease,
        error_code="x", error_message="A's late failure",
    )
    assert settled is False

    row = repo._runtime.notebook_delete_jobs.get(job["id"])
    assert row["status"] == "running"  # B's row, untouched
    assert row["lease_token"] == fresh_lease
    assert row["attempts"] == 0  # A's finish must not have consumed a retry


def test_finish_residual_is_a_noop_when_the_lease_no_longer_matches(repo):
    """P2-b: same fencing, residual-cleanup path — a fenced-out call leaves
    BOTH `notebook_delete_jobs` and `notebook_delete_files` untouched."""
    _seed_user_and_notebook(repo)
    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    stale_lease = repo._runtime.notebook_delete_jobs.mark_running(
        job["id"], stale_cutoff_seconds=300
    )
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO notebook_delete_files(job_id,ordinal,file_path) "
            "VALUES (?,?,?)",
            (job["id"], 0, "/tmp/whatever"),
        )
        db.execute(
            "UPDATE notebook_delete_jobs SET updated_at=? WHERE id=?",
            ("2020-01-01T00:00:00", job["id"]),
        )
    fresh_lease = repo._runtime.notebook_delete_jobs.mark_running(
        job["id"], stale_cutoff_seconds=300
    )
    assert fresh_lease is not None and fresh_lease != stale_lease

    settled = repo._runtime.notebook_delete_jobs.finish_residual(
        job["id"], "nb1", lease_token=stale_lease,
    )
    assert settled is False

    row = repo._runtime.notebook_delete_jobs.get(job["id"])
    assert row["lease_token"] == fresh_lease  # B's row, untouched
    with repo._runtime.database.connect() as db:
        remaining = db.execute(
            "SELECT COUNT(*) AS c FROM notebook_delete_files WHERE job_id=?",
            (job["id"],),
        ).fetchone()
    assert remaining["c"] == 1  # side-table row untouched too, not half-deleted

    # B, holding the real lease, settles it normally.
    settled = repo._runtime.notebook_delete_jobs.finish_residual(
        job["id"], "nb1", lease_token=fresh_lease,
    )
    assert settled is True
    with pytest.raises(KeyError):
        repo._runtime.notebook_delete_jobs.get(job["id"])


# ---------------------------------------------------------------------------
# P2-b: closure reconciliation — every table with a notebook_id column (or
# an FK path to notebooks) is accounted for by phase 3, the 5 phase-5 tables
# (4 archive-input + answers), or the 2 D-class tables.
# ---------------------------------------------------------------------------


_PHASE_5_TABLES = frozenset({"ask_jobs", "sources", "reports", "source_paper_meta", "answers"})
_D_CLASS_TABLES = frozenset({"object_schemas", "retained_user_activity"})
_OWN_BOOKKEEPING = frozenset({"notebooks", "notebook_delete_jobs", "notebook_delete_files"})
# Global/admin tables genuinely outside the notebook-delete closure --
# either not notebook-scoped at all, or scoped to something else entirely
# (users/agent identity, not notebooks).
_NOT_NOTEBOOK_SCOPED = frozenset({
    "users", "user_profiles", "auth_sessions", "app_settings", "groups",
    "group_members", "agent_profiles", "model_service_status",
    "system_model_service_status", "extension_runtime_toggles",
    "concept_whitelist", "retrieval_experiences", "wishes", "wish_votes",
})


def test_every_notebook_scoped_table_is_accounted_for(repo):
    """P2-b:用真实 SQLite schema 內省算出「有 notebook_id 列的表集合」,断言它
    恰好等于 phase_3_table_names() ∪ 五张相位 5 表 ∪ 两张 D 类表 ∪ 自身簿记
    ——名字集合断言，不是纯计数（少一张表也能被抓到，不像总数对不上才报警）。
    """
    with repo._runtime.database.connect() as db:
        tables = [
            row["name"] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        notebook_scoped = set()
        for table in tables:
            if table in _NOT_NOTEBOOK_SCOPED:
                continue
            if table.endswith("_fts") or "_fts_" in table:
                continue  # FTS5 shadow tables (config/data/idx/...), not real tables
            columns = {
                row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if "notebook_id" in columns or table in _OWN_BOOKKEEPING:
                notebook_scoped.add(table)

    accounted = (
        ndt.phase_3_table_names() | _PHASE_5_TABLES | _D_CLASS_TABLES | _OWN_BOOKKEEPING
    )
    missing = notebook_scoped - accounted
    assert not missing, f"tables with notebook_id unaccounted for: {sorted(missing)}"


# ---------------------------------------------------------------------------
# P3: _artifact_siblings and ScaleArtifactStore.indexed_notebook_ids must
# keep agreeing on "what counts as a scratch/rollback sibling" — they now
# share SCRATCH_SUFFIXES/SCRATCH_INFIX, this test proves the two independent
# classification code paths still land on the same verdict for every
# boundary name, not just that they import the same constants.
# ---------------------------------------------------------------------------


def test_artifact_siblings_and_indexed_notebook_ids_share_the_same_scratch_shape(
    tmp_path,
):
    from app.repositories.filesystem.scale_artifact_store import (
        SCRATCH_INFIX,
        SCRATCH_SUFFIXES,
    )

    notebook_id = "nb-shape"
    other_id = "nb-other"
    names = [
        notebook_id,  # live
        f"{notebook_id}.old",
        f"{notebook_id}.tmp",
        f"{notebook_id}.tmp-abc123",
        f"{notebook_id}.tmp-def456",
        other_id,  # a different notebook's live dir must never match
        f"{other_id}.old",
    ]
    root = tmp_path / "kg_index"
    root.mkdir()
    for name in names:
        directory = root / name
        directory.mkdir()
        (directory / "manifest.json").write_text("{}")

    # indexed_notebook_ids()'s own classification: "is this name a scratch
    # sibling of ANY notebook" (suffix/infix only, no notebook_id scoping).
    def is_scratch_by_indexed_notebook_ids_shape(name: str) -> bool:
        return name.endswith(SCRATCH_SUFFIXES) or SCRATCH_INFIX in name

    # _artifact_siblings()'s own classification, scoped to notebook_id: which
    # of the 4 buckets (tmp-dash / tmp / old / live) each entry falls into.
    siblings = nd._artifact_siblings(root, notebook_id)
    sibling_names = {p.name for p in siblings}
    assert sibling_names == {
        f"{notebook_id}.tmp-abc123",
        f"{notebook_id}.tmp-def456",
        f"{notebook_id}.tmp",
        f"{notebook_id}.old",
        notebook_id,
    }
    # Every name _artifact_siblings classified as a scratch variant (not the
    # live dir) must ALSO be excluded by indexed_notebook_ids' filter, and
    # vice versa for this notebook's own names — the two must never diverge.
    for name in names:
        if not name.startswith(notebook_id):
            continue
        is_scratch_here = name != notebook_id
        assert is_scratch_by_indexed_notebook_ids_shape(name) == is_scratch_here, (
            f"{name!r}: indexed_notebook_ids shape says scratch="
            f"{is_scratch_by_indexed_notebook_ids_shape(name)}, but "
            f"_artifact_siblings treats it as scratch={is_scratch_here}"
        )

    from app.repositories.filesystem.scale_artifact_store import ScaleArtifactStore

    class _Settings:
        storage_dir = str(tmp_path)

    published = ScaleArtifactStore(_Settings()).indexed_notebook_ids()
    assert published == sorted([notebook_id, other_id])


# ---------------------------------------------------------------------------
# codex #659 R2: in-process submission dedupe — a job parked in the delete
# pool's local queue must not be re-submitted by every sweep tick while one
# long delete occupies the only slot.
# ---------------------------------------------------------------------------


def test_submit_dedupes_jobs_already_queued_in_this_process(repo, monkeypatch):
    """变异钉：去掉 ``_submit`` 的 ``_inflight`` 去重集合 → 第二次提交也进
    池,captured 变成 2,本条报红。完成(``_run_submitted`` 的 finally)之后
    重新提交必须再次可行——去重只覆盖「仍在队里或在跑」的窗口。"""
    from app.services import background_jobs

    runner = repo._runtime.notebook_delete
    captured = []
    monkeypatch.setattr(
        background_jobs, "submit",
        lambda fn, *args, **kwargs: captured.append((fn, args)),
    )

    assert runner._submit("job-1", "nb-x") is True
    assert runner._submit("job-1", "nb-x") is False, (
        "a job already handed to this process's pool must not be re-submitted"
    )
    assert len(captured) == 1

    # Simulate the pooled execution finishing: run() is a no-op for an
    # unknown job id, and the finally block must clear the in-flight mark.
    fn, args = captured[0]
    fn(*args)
    assert runner._submit("job-1", "nb-x") is True


# ---------------------------------------------------------------------------
# codex #659 R3 P2: request()'s 202 contract must survive background_jobs.
# submit() raising (pool saturated, interpreter shutting down) — the
# tombstone + job row are already committed by that point, so re-raising
# would turn an already-durable success into a 500 whose retry then 404s/
# 409s against the now-'deleting' notebook.
# ---------------------------------------------------------------------------


def test_request_stays_202_when_background_jobs_submit_raises(repo, monkeypatch):
    """变异钉：把 ``_submit`` 的 ``except Exception`` 去掉 → submit() 的
    RuntimeError 直接冒穿 request(),本条报红。"""
    from app.services import background_jobs

    _seed_user_and_notebook(repo)

    def _boom(fn, *args, **kwargs):
        raise RuntimeError("pool saturated")

    monkeypatch.setattr(background_jobs, "submit", _boom)

    result = repo._runtime.notebook_delete.request("nb1", "u1")
    assert result == {"status": "deleting"}

    row = repo._runtime.notebook_delete_jobs.latest_for_notebook("nb1")
    assert row is not None
    assert row["status"] == "queued"  # untouched — never actually got to run()

    # get_row()'s NOTEBOOK_LIVE_SQL filter hides 'deleting' rows on purpose
    # (that IS the tombstone's whole point) — read the raw column instead.
    with repo._runtime.database.connect() as db:
        nb_row = db.execute(
            "SELECT status FROM notebooks WHERE id='nb1'"
        ).fetchone()
    assert nb_row["status"] == "deleting"  # tombstone stands


def test_request_stays_202_and_the_stuck_job_is_later_swept(repo, monkeypatch):
    """submit() 抛错留下的 queued 行不是死路——扫尾驱动 A(陈旧活跃行)按常规
    节奏把它接过去正常跑完。"""
    from app.services import background_jobs

    _seed_user_and_notebook(repo)
    monkeypatch.setattr(
        background_jobs, "submit",
        lambda fn, *a, **k: (_ for _ in ()).throw(RuntimeError("pool saturated")),
    )
    result = repo._runtime.notebook_delete.request("nb1", "u1")
    assert result == {"status": "deleting"}
    job = repo._runtime.notebook_delete_jobs.latest_for_notebook("nb1")

    # Simulate time passing without this job ever having actually run:
    # backdate updated_at past the sweep's own stale cutoff so driver A picks
    # it up (the job never got a chance to update it in the first place —
    # this only removes the dependency on real wall-clock time in the test).
    with repo._runtime.database.write() as db:
        db.execute(
            "UPDATE notebook_delete_jobs SET updated_at=? WHERE id=?",
            ("2020-01-01T00:00:00", job["id"]),
        )
    monkeypatch.undo()  # restore the real background_jobs.submit
    runner = repo._runtime.notebook_delete
    runner._sweep_seconds = 1
    assert runner.sweep_once() == 1
    background_jobs._drain_maintenance_executors_for_tests(timeout=10.0)

    with pytest.raises(KeyError):
        repo._runtime.notebook_delete_jobs.get(job["id"])  # finalize deleted it
    with repo._runtime.database.connect() as db:
        remaining = db.execute(
            "SELECT COUNT(*) AS c FROM notebooks WHERE id='nb1'"
        ).fetchone()
    assert remaining["c"] == 0


# ---------------------------------------------------------------------------
# codex #659 R3 P1 (1/2): a straggler upload/reparse that lands its file
# AFTER phase 4's disk sweep has already run must still be cleaned up — the
# exclusive claim (§4.3) excludes a concurrent scale BUILD, never ingestion.
# ---------------------------------------------------------------------------


def test_finalize_sweeps_ingestion_stragglers_that_land_after_phase_4(repo, monkeypatch):
    """在相位 4 已经跑完、相位 5 尚未提交这个窗口里模拟一次「已经过了
    get_notebook 检查」的在途摄取真正把文件落到盘上——两棵目录（源文件/贴图
    资产）各塞一个 straggler 文件,断言 finalize 提交之后这两棵目录本身
    (不只是塞进去的文件)都不再存在。变异钉：去掉 ``_sweep_ingestion_
    stragglers`` 在 ``_phase_finalize`` 里的调用 → 两棵目录原样留着,报红。"""
    _seed_user_and_notebook(repo)
    runner = repo._runtime.notebook_delete
    storage_dir = runner._storage_dir()
    source_dir = storage_dir / "notebooks" / "nb1"
    asset_dir = storage_dir / "assets" / "nb1"
    source_dir.mkdir(parents=True)
    asset_dir.mkdir(parents=True)
    straggler_source = source_dir / "straggler-upload.pdf"
    straggler_asset = asset_dir / "straggler-image.jpg"
    straggler_source.write_bytes(b"late upload bytes")
    straggler_asset.write_bytes(b"late pasted image bytes")

    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    runner.run(job["id"])

    with pytest.raises(KeyError):
        repo._runtime.notebook_delete_jobs.get(job["id"])  # finalize completed
    assert not source_dir.exists(), "straggler 源文件目录本该被整目录清扫删掉"
    assert not asset_dir.exists(), "straggler 贴图资产目录本该被整目录清扫删掉"


def test_residual_cleanup_also_sweeps_ingestion_stragglers(repo):
    """残渣收尾路径（notebooks 行已经带外消失）同样要跑这次整目录清扫——
    notebooks 行在这条路径里更早就已经不在了,摄取 straggler 的风险不比
    正常收尾路径小。"""
    _seed_user_and_notebook(repo)
    runner = repo._runtime.notebook_delete
    storage_dir = runner._storage_dir()
    source_dir = storage_dir / "notebooks" / "nb1"
    source_dir.mkdir(parents=True)
    (source_dir / "straggler.pdf").write_bytes(b"x")

    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    with repo._runtime.database.write() as db:
        db.execute("DELETE FROM notebooks WHERE id='nb1'")  # out-of-band delete

    runner.run(job["id"])

    with pytest.raises(KeyError):
        repo._runtime.notebook_delete_jobs.get(job["id"])
    assert not source_dir.exists()


# ---------------------------------------------------------------------------
# codex #659 R3 P1 (2/2): upload_sources' row-insert failure (notebook
# deleted mid-request → FK violation, or any other exception) must not leave
# the just-written file behind — a compensating unlink.
# ---------------------------------------------------------------------------


def test_upload_sources_unlinks_the_just_written_file_when_the_row_insert_fails(
    repo, monkeypatch,
):
    """变异钉：把 ``except Exception`` 收窄回 ``except DocumentCapacityExceeded``
    → 本用例模拟的是一个不同的异常类型,行插入失败后文件原样留着,报红。"""
    _seed_user_and_notebook(repo)
    ingestion = repo._runtime.source_ingestion
    from app.services.source_ingestion import UploadedSourceFile

    def _boom(*args, **kwargs):
        raise RuntimeError("notebook FK violation (simulated)")

    monkeypatch.setattr(ingestion, "_insert_uploaded_source", _boom)

    with pytest.raises(RuntimeError):
        ingestion.upload_sources(
            "nb1",
            [UploadedSourceFile(
                file_name="doc.pdf", content_type="application/pdf",
                content=b"hello world",
            )],
            None, ingestion.pipeline_hooks(),
        )

    storage_dir = repo._runtime.notebook_delete._storage_dir()
    source_dir = storage_dir / "notebooks" / "nb1"
    leftover = list(source_dir.iterdir()) if source_dir.exists() else []
    assert leftover == [], f"补偿删除应当清空刚落盘的孤儿文件,残留：{leftover}"


def test_a_failed_recreate_keeps_the_attempts_history_intact(repo, monkeypatch):
    """codex #659 R4：purge 与替换插入必须同事务——插入失败(此处注入
    IntegrityError:new_id 撞既有主键)时旧 failed 行连同 attempts/finished_at
    必须原样回滚保留,否则下一次 sweep 把这本库当第一次尝试,退避与五次上限
    双双失效。变异钉:把 purge 拆回 recreate 事务之外→本条红(旧行已被单独
    提交的 purge 删掉)。"""
    _seed_user_and_notebook(repo, status="deleting")
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO notebook_delete_jobs (id,notebook_id,status,phase,"
            "cursor_table,cursor_key,deleted_rows,lease_token,attempts,"
            "error_code,error_message,created_at,updated_at,finished_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ndj-old", "nb1", "failed", "mark", "", "", 0, "", 3,
                "notebook_delete_failed", "boom", NOW, NOW, NOW,
            ),
        )

    store = repo._runtime.notebook_delete_jobs
    # 让替换行的 INSERT 撞上旧行的主键——事务里 purge 已把 ndj-old 删掉,
    # 但 INSERT 用同一个 id 仍然……不行,purge 删了它就不冲突了;改成撞一个
    # 独立的活跃行的唯一索引:再造一本库占用同 id 更绕。最直接:new_id 返回
    # 一个已存在于**另一本库**的作业行主键。
    _seed_user_and_notebook(repo, notebook_id="nb2", owner="u2", status="deleting")
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO notebook_delete_jobs (id,notebook_id,status,phase,"
            "cursor_table,cursor_key,deleted_rows,lease_token,attempts,"
            "error_code,error_message,created_at,updated_at,finished_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ndj-clash", "nb2", "failed", "mark", "", "", 0, "", 1,
                "notebook_delete_failed", "x", NOW, NOW, NOW,
            ),
        )
    monkeypatch.setattr(store, "new_id", lambda _prefix: "ndj-clash")

    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        store.recreate_for_deleting_notebook("nb1", attempts=3)

    with repo._runtime.database.connect() as db:
        row = db.execute(
            "SELECT attempts, finished_at FROM notebook_delete_jobs "
            "WHERE id='ndj-old'"
        ).fetchone()
        assert row is not None, (
            "the only row carrying the retry history must survive a failed "
            "replacement insert"
        )
        assert row["attempts"] == 3


# ---------------------------------------------------------------------------
# codex #659 R5: the SQLite FTS5 shadow cleanup is paged (rowid keyset), not
# one unbounded DELETE holding the single-writer lock for a giant tx.
# ---------------------------------------------------------------------------


def test_fts_shadow_cleanup_is_paged_and_scoped(repo):
    """变异钉:把 delete_fts_shadow_page 改回单条无界 DELETE(或忽略 limit)
    →「每页 deleted<=limit」断言红。同时钉住:只删本库行、循环收敛到零、
    其它库的影子行原样。"""
    store = repo._runtime.notebook_delete_jobs
    with repo._runtime.database.write() as db:
        for i in range(7):
            db.execute(
                "INSERT INTO chunks_fts (chunk_id,notebook_id,text) "
                "VALUES (?,?,?)",
                (f"ch-{i}", "nb1", f"text {i}"),
            )
        db.execute(
            "INSERT INTO chunks_fts (chunk_id,notebook_id,text) "
            "VALUES ('ch-other','nb2','keep me')",
        )

    cursor = 0
    total = 0
    rounds = 0
    while True:
        deleted, cursor = store.delete_fts_shadow_page(
            "chunks", "nb1", cursor, 3
        )
        assert deleted <= 3, "one page must never exceed its limit"
        if deleted == 0:
            break
        total += deleted
        rounds += 1
        assert rounds <= 10, "the paged loop must converge"

    assert total == 7
    assert rounds == 3  # 3+3+1: bounded pages, not one giant delete
    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) c FROM chunks_fts WHERE notebook_id='nb1'"
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) c FROM chunks_fts WHERE notebook_id='nb2'"
        ).fetchone()["c"] == 1
    # A table with no shadow is a structural no-op.
    assert store.delete_fts_shadow_page("sources", "nb1", 0, 3) == (0, 0)


# ---------------------------------------------------------------------------
# codex #659 R6 P2: conversations has no FK to notebooks and is swept only
# ONCE by phase 3 — a row inserted after that sweep but before phase 5's
# finalize survives forever. Two layers: ensure_conversation now rejects
# inserting for a non-live notebook; finalize/residual-cleanup sweep any
# straggler that slipped in anyway.
# ---------------------------------------------------------------------------


def test_ensure_conversation_refuses_to_insert_once_the_tombstone_has_landed(repo):
    """变异钉：把 INSERT...SELECT...WHERE EXISTS 形回退成普通 INSERT →
    本条不再抛 KeyError，报红。"""
    _seed_user_and_notebook(repo)
    with repo._runtime.database.write() as db:
        db.execute("UPDATE notebooks SET status='deleting' WHERE id='nb1'")

    with pytest.raises(KeyError):
        with repo._runtime.database.write() as db:
            repo._runtime.ask_state.ensure_conversation(
                db, "nb1", None, "问题", "u1",
            )

    with repo._runtime.database.connect() as db:
        remaining = db.execute(
            "SELECT COUNT(*) AS c FROM conversations WHERE notebook_id='nb1'"
        ).fetchone()
    assert remaining["c"] == 0  # 没有半途插入的行


def test_ensure_conversation_still_inserts_for_a_live_notebook(repo):
    """守卫不能连正常路径也一起挡了。"""
    _seed_user_and_notebook(repo)
    with repo._runtime.database.write() as db:
        conversation_id = repo._runtime.ask_state.ensure_conversation(
            db, "nb1", None, "问题", "u1",
        )
    assert conversation_id
    with repo._runtime.database.connect() as db:
        row = db.execute(
            "SELECT id FROM conversations WHERE id=?", (conversation_id,)
        ).fetchone()
    assert row is not None


class _AlwaysHeldClaim:
    def verify_held(self):
        return True

    def release(self):
        pass


def test_finalize_sweeps_a_conversations_straggler_that_slipped_past_phase_3(repo):
    """精确模拟"相位 3 扫完 conversations 之后、相位 5 之前又插进来一行"这道
    时间窗:先真正跑完相位 3(此时 conversations 干净),再直接落一行带外
    straggler(模拟一次在两者之间落地的写),再直接调 ``_phase_finalize``。
    变异钉:去掉 delete_row_and_orphan_embeddings 里那条 DELETE FROM
    conversations → 这一行留到最后,报红。"""
    _seed_user_and_notebook(repo)
    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    lease_token = repo._runtime.notebook_delete_jobs.mark_running(
        job["id"], stale_cutoff_seconds=300,
    )
    runner = repo._runtime.notebook_delete
    finished = runner._phase_rows(
        job["id"], "nb1", lease_token, _AlwaysHeldClaim(), residual=False,
    )
    assert finished is True

    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO conversations (id,notebook_id,title,created_by,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?)",
            ("conv-straggler", "nb1", "T", "u1", NOW, NOW),
        )

    runner._phase_finalize(job["id"], "nb1", lease_token)

    with repo._runtime.database.connect() as db:
        remaining = db.execute(
            "SELECT COUNT(*) AS c FROM conversations WHERE notebook_id='nb1'"
        ).fetchone()
    assert remaining["c"] == 0


# ---------------------------------------------------------------------------
# codex #659 R14 P2: phase 4's rmtree can run long enough past
# stale_cutoff_seconds with no intervening heartbeat that sweep driver A's
# mark_running steals the lease AFTER the pre-finalize _batch_ok recheck
# passes but BEFORE finalize's own transaction commits. cleanup_job_on's
# job-row DELETE now carries AND lease_token=? — rowcount=0 with the row
# still there raises StaleLeaseFinalizeError, rolling the WHOLE finalize
# transaction back atomically (the notebooks row DELETE staged earlier in
# the SAME transaction is undone with it), so the new owner can later
# finish the SAME job cleanly instead of racing a half-committed delete.
# ---------------------------------------------------------------------------


def test_pre_finalize_recheck_catches_a_stolen_lease_the_claim_alone_cannot_see(repo):
    """codex #659 R14 P2 layer 1: a bare ``claim.verify_held()`` (the OLD
    pre-finalize gate) only proves the old worker's OWN advisory claim is
    genuinely still intact — it says nothing about whether ownership of the
    JOB ROW itself has moved to a new worker (sweep driver A stealing a
    stale-looking lease during a long phase-4 rmtree). ``_batch_ok``'s
    ``ownership_snapshot`` half must catch this even when
    ``claim.verify_held()`` alone would report True."""
    _seed_user_and_notebook(repo)
    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    old_lease = repo._runtime.notebook_delete_jobs.mark_running(
        job["id"], stale_cutoff_seconds=300,
    )
    with repo._runtime.database.write() as db:
        db.execute(
            "UPDATE notebook_delete_jobs SET updated_at='2000-01-01T00:00:00' "
            "WHERE id=?", (job["id"],),
        )
    new_lease = repo._runtime.notebook_delete_jobs.mark_running(
        job["id"], stale_cutoff_seconds=1,
    )
    assert new_lease is not None and new_lease != old_lease

    runner = repo._runtime.notebook_delete
    claim = _AlwaysHeldClaim()
    assert claim.verify_held() is True  # sanity: the claim alone sees nothing wrong
    assert runner._batch_ok(job["id"], old_lease, claim, residual=False) is False  # MUT


def test_run_stops_before_finalize_when_a_concurrent_worker_steals_the_lease(
    repo, monkeypatch,
):
    """Same gap as above, exercised through the REAL ``run()`` orchestration
    (not a direct ``_batch_ok`` call) with the REAL claim wiring (the SQLite
    scale-build-lock advisory claim, not a stub) — proving the wiring change
    at the phase == 'files' call site, not just _batch_ok's own contract.
    Reproducing "a concurrent worker steals the lease mid-run()" with real
    concurrency would need threads; instead, ``advance_phase`` (the call
    that lands the 'rows' -> 'files' transition) is wrapped to inject the
    steal at that exact interleaving point, once, immediately after it does
    its own real work. ``claim.verify_held()`` alone stays True throughout
    (nothing touches the SEPARATE scale-build-lock this claim guards) — only
    _batch_ok's ownership half can stop this run() before finalize.

    ``_phase_finalize`` is spied on (not just "did the notebook survive")
    because layer 2's transaction-level fence (cleanup_job_on) ALSO
    guarantees the notebook survives even if layer 1 here is bypassed — a
    pure survival assertion cannot tell the two layers apart. This test
    must fail specifically when LAYER 1 is removed, i.e. _phase_finalize
    must never even be ENTERED, not merely "entered but safely rolled
    back" (that half is covered by
    ``test_finalize_transaction_rolls_back_atomically_when_the_lease_was_
    stolen`` below). "新主接手收敛" is also covered there (a second
    ``run()`` here would just re-attempt ``mark_running`` against the
    brand-new, still-fresh lease this test just minted and correctly
    no-op — reaching an actually-independent second owner needs a real
    second process/thread, not a re-entrant call in this one)."""
    _seed_user_and_notebook(repo)
    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    runner = repo._runtime.notebook_delete
    store = repo._runtime.notebook_delete_jobs
    original_advance_phase = store.advance_phase
    stolen = {}

    def advance_phase_then_maybe_steal(job_id, phase, **kwargs):
        result = original_advance_phase(job_id, phase, **kwargs)
        if phase == "files" and not stolen:
            with repo._runtime.database.write() as db:
                db.execute(
                    "UPDATE notebook_delete_jobs SET updated_at='2000-01-01T00:00:00' "
                    "WHERE id=?", (job_id,),
                )
            stolen["lease"] = store.mark_running(job_id, stale_cutoff_seconds=1)
            assert stolen["lease"] is not None
        return result

    monkeypatch.setattr(store, "advance_phase", advance_phase_then_maybe_steal)
    finalize_calls = []
    original_phase_finalize = type(runner)._phase_finalize
    monkeypatch.setattr(
        type(runner), "_phase_finalize",
        lambda self, *a, **k: finalize_calls.append((a, k)) or original_phase_finalize(self, *a, **k),
    )
    runner.run(job["id"])  # MUT
    assert finalize_calls == []  # never even entered — not "entered, then rolled back"

    with repo._runtime.database.connect() as db:
        notebook_row = db.execute("SELECT 1 FROM notebooks WHERE id='nb1'").fetchone()
        job_row = db.execute(
            "SELECT lease_token FROM notebook_delete_jobs WHERE id=?", (job["id"],)
        ).fetchone()
    assert notebook_row is not None  # finalize never ran
    assert job_row is not None and job_row["lease_token"] == stolen["lease"]


def test_finalize_transaction_rolls_back_atomically_when_the_lease_was_stolen(repo):
    _seed_user_and_notebook(repo)
    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    old_lease = repo._runtime.notebook_delete_jobs.mark_running(
        job["id"], stale_cutoff_seconds=300,
    )
    runner = repo._runtime.notebook_delete
    finished = runner._phase_rows(
        job["id"], "nb1", old_lease, _AlwaysHeldClaim(), residual=False,
    )
    assert finished is True
    # Simulate phase 4's rmtree running long enough that updated_at went
    # stale with no heartbeat, and sweep driver A's mark_running stealing
    # the lease out from under the old worker.
    with repo._runtime.database.write() as db:
        db.execute(
            "UPDATE notebook_delete_jobs SET updated_at='2000-01-01T00:00:00' "
            "WHERE id=?", (job["id"],),
        )
    new_lease = repo._runtime.notebook_delete_jobs.mark_running(
        job["id"], stale_cutoff_seconds=1,
    )
    assert new_lease is not None
    assert new_lease != old_lease

    # The old worker, unaware its lease was stolen, still tries to finalize
    # with its now-stale lease_token.
    with pytest.raises(StaleLeaseFinalizeError):
        runner._phase_finalize(job["id"], "nb1", old_lease)  # MUT

    with repo._runtime.database.connect() as db:
        notebook_row = db.execute(
            "SELECT status FROM notebooks WHERE id='nb1'"
        ).fetchone()
        job_row = db.execute(
            "SELECT lease_token FROM notebook_delete_jobs WHERE id=?", (job["id"],)
        ).fetchone()
    assert notebook_row is not None  # DELETE FROM notebooks was rolled back
    assert job_row is not None and job_row["lease_token"] == new_lease  # untouched

    # The new owner independently converges the SAME job with its own lease.
    runner._phase_finalize(job["id"], "nb1", new_lease)
    with repo._runtime.database.connect() as db:
        gone = db.execute("SELECT 1 FROM notebooks WHERE id='nb1'").fetchone()
        job_gone = db.execute(
            "SELECT 1 FROM notebook_delete_jobs WHERE id=?", (job["id"],)
        ).fetchone()
    assert gone is None
    assert job_gone is None


def test_residual_cleanup_sweeps_a_conversations_straggler_too(repo):
    """残渣收尾路径同样要清 conversations——notebooks 行在这条路径里更早
    就已经不在了。同样先跑完相位 3、再落带外 straggler、再直接调
    ``_finish_residual``。"""
    _seed_user_and_notebook(repo)
    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    lease_token = repo._runtime.notebook_delete_jobs.mark_running(
        job["id"], stale_cutoff_seconds=300,
    )
    runner = repo._runtime.notebook_delete
    finished = runner._phase_rows(
        job["id"], "nb1", lease_token, _AlwaysHeldClaim(), residual=False,
    )
    assert finished is True

    with repo._runtime.database.write() as db:
        db.execute("DELETE FROM notebooks WHERE id='nb1'")  # out-of-band delete
        db.execute(
            "INSERT INTO conversations (id,notebook_id,title,created_by,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?)",
            ("conv-straggler", "nb1", "T", "u1", NOW, NOW),
        )

    runner._finish_residual(job["id"], "nb1", lease_token)

    with pytest.raises(KeyError):
        repo._runtime.notebook_delete_jobs.get(job["id"])
    with repo._runtime.database.connect() as db:
        remaining = db.execute(
            "SELECT COUNT(*) AS c FROM conversations WHERE notebook_id='nb1'"
        ).fetchone()
    assert remaining["c"] == 0


# ---------------------------------------------------------------------------
# codex #659 R8 P1: _drain_children_by_parent_ids commits several
# independent sub-batches per call (each its own transaction) — the runner
# only checked ownership/claim ONCE, before the whole call, never between
# those sub-batches. A page whose total fanout outlasts the sweep's stale
# cutoff could have its lease stolen mid-drain while still issuing real,
# separately-committed DELETEs under a lease it no longer holds.
# ---------------------------------------------------------------------------


def _seed_source_with_elements(repo, notebook_id, source_id, count):
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,file_path,source_type,"
            "status,parse_status,uploaded_by,created_at,updated_at) VALUES "
            "(?,?,?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, "S", "", "pdf", "ready", "parsed", "u1", NOW, NOW),
        )
        for index in range(count):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,"
                "location_label,text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
                (f"{source_id}-el{index}", source_id, "paragraph", "p", "x", "{}", NOW),
            )


def test_drain_children_by_parent_ids_gates_every_sub_batch_but_not_the_final_empty_probe(
    repo,
):
    """计数桩断言:7 个子行、批大小 3(3+3+1)→ gate 恰好被调用 3 次——一次
    在第 1/2 批提交之后、一次在第 2/3 批提交之后、一次在第 3 批提交之后
    （发现「没有更多了」的第 4 次探测式 DELETE 命中 rowcount==0，不需要再
    问 gate，因为那一轮什么都没删,不构成一次有所有权含义的破坏性子批）。
    变异钉：去掉 gate 调用 → calls 变成 []，本条报红。"""
    _seed_user_and_notebook(repo)
    _seed_source_with_elements(repo, "nb1", "src-1", 7)
    store = repo._runtime.notebook_delete_jobs
    store._CHILD_BATCH_SIZE = 3

    calls = []

    def counting_gate():
        calls.append(len(calls))
        return True

    drained = store._drain_children_by_parent_ids(
        "source_elements", "source_id", ["src-1"], batch_ok=counting_gate,
    )
    assert drained is True
    assert len(calls) == 3

    with repo._runtime.database.connect() as db:
        remaining = db.execute(
            "SELECT COUNT(*) AS c FROM source_elements WHERE source_id='src-1'"
        ).fetchone()
    assert remaining["c"] == 0


def test_drain_children_by_parent_ids_gates_none_when_never_given_one(repo):
    """变异钉的另一半：``batch_ok=None``(既有调用方/测试的默认值)必须继续
    一次不问地把 7 行全部删完——本参数存在之前的行为逐字不变。"""
    _seed_user_and_notebook(repo)
    _seed_source_with_elements(repo, "nb1", "src-1", 7)
    store = repo._runtime.notebook_delete_jobs
    store._CHILD_BATCH_SIZE = 3

    drained = store._drain_children_by_parent_ids(
        "source_elements", "source_id", ["src-1"],
    )
    assert drained is True
    with repo._runtime.database.connect() as db:
        remaining = db.execute(
            "SELECT COUNT(*) AS c FROM source_elements WHERE source_id='src-1'"
        ).fetchone()
    assert remaining["c"] == 0


def test_drain_children_by_parent_ids_stops_mid_drain_when_the_gate_fails_then_converges(
    repo,
):
    """gate 第二次返回 False → 排水停在中途(第 1、2 批的 6 行已经各自独立
    提交,gate 在第 2 批提交之后才报 False,第 3 批还没来得及提交就被拦下)、
    剩余 1 行仍在;之后换一个恒真的 gate 重跑，从"还剩多少"的同一条查询
    自然收敛到 0——不依赖任何持久化的子批游标。"""
    _seed_user_and_notebook(repo)
    _seed_source_with_elements(repo, "nb1", "src-1", 7)
    store = repo._runtime.notebook_delete_jobs
    store._CHILD_BATCH_SIZE = 3

    calls = []

    def fails_on_second_call():
        calls.append(len(calls))
        return len(calls) < 2

    drained = store._drain_children_by_parent_ids(
        "source_elements", "source_id", ["src-1"], batch_ok=fails_on_second_call,
    )
    assert drained is False
    assert len(calls) == 2

    with repo._runtime.database.connect() as db:
        remaining = db.execute(
            "SELECT COUNT(*) AS c FROM source_elements WHERE source_id='src-1'"
        ).fetchone()
    assert remaining["c"] == 1  # 7 - 3 - 3 (first two sub-batches already committed)

    # A later resume with an always-true gate converges to zero — no
    # persisted sub-batch cursor needed, just "any rows left" re-queried.
    drained_again = store._drain_children_by_parent_ids(
        "source_elements", "source_id", ["src-1"], batch_ok=lambda: True,
    )
    assert drained_again is True
    with repo._runtime.database.connect() as db:
        remaining = db.execute(
            "SELECT COUNT(*) AS c FROM source_elements WHERE source_id='src-1'"
        ).fetchone()
    assert remaining["c"] == 0


def test_run_chain_does_not_advance_the_cursor_when_the_sub_batch_gate_fails(repo):
    """端到端接线:``_run_chain`` 传给 ``delete_source_elements_page`` 的
    ``batch_ok`` 复用 ``_batch_ok``(同一份租/claim 复验)。claim 在第 2 个
    子批提交后失效 → 排水中途停手、父页游标不前进、``_run_chain`` 的下一轮
    自身的 ``_batch_ok`` 立即也失败(同一份丢失的所有权),整条链返回
    False,而不是带着丢失的所有权继续推进到下一页父 id。"""
    _seed_user_and_notebook(repo)
    _seed_source_with_elements(repo, "nb1", "src-1", 7)
    store = repo._runtime.notebook_delete_jobs
    store._CHILD_BATCH_SIZE = 3

    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    lease_token = repo._runtime.notebook_delete_jobs.mark_running(
        job["id"], stale_cutoff_seconds=300,
    )
    runner = repo._runtime.notebook_delete

    class _ClaimThatFailsAfterTwoChecks:
        def __init__(self):
            self.checks = 0

        def verify_held(self):
            self.checks += 1
            return self.checks <= 2

        def release(self):
            pass

    claim = _ClaimThatFailsAfterTwoChecks()
    finished = runner._run_chain(
        job["id"], "nb1", lease_token, claim, "cursor_source_elements",
        "source_elements", "", residual=False,
    )
    assert finished is False

    row = repo._runtime.notebook_delete_jobs.get(job["id"])
    # The cursor never advanced past the (single) parent page whose children
    # are still incomplete.
    assert row["cursor_key"] == ""
    with repo._runtime.database.connect() as db:
        remaining = db.execute(
            "SELECT COUNT(*) AS c FROM source_elements WHERE source_id='src-1'"
        ).fetchone()
    assert 0 < remaining["c"] < 7  # partially drained, not zero, not untouched


# ---------------------------------------------------------------------------
# codex #659 R9 P2: delete_knowhow_tables_page deleted a page's full
# knowhow_columns/knowhow_changes/knowhow_milestones in ONE transaction — a
# page of up to 500 tables' COMBINED child rows is itself unbounded even
# though each individual table's own count is small, risking PostgreSQL's
# statement_timeout / holding SQLite's write lock for the whole page. Same
# fix as R8: drain each child table via _drain_children_by_parent_ids
# (batch_ok-gated), only deleting the parent knowhow_tables rows once all
# three child tables are confirmed fully drained.
# ---------------------------------------------------------------------------


def _seed_knowhow_table_with_columns_and_changes(
    repo, notebook_id, table_id, n_columns, n_changes,
):
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO knowhow_tables (id,notebook_id,title,description,"
            "mutation_seq,created_by,created_at,updated_at) VALUES "
            "(?,?,?,?,?,?,?,?)",
            (table_id, notebook_id, "KT", "", 0, "u1", NOW, NOW),
        )
        for index in range(n_columns):
            db.execute(
                "INSERT INTO knowhow_columns (id,table_id,name,role,position) "
                "VALUES (?,?,?,?,?)",
                (f"{table_id}-col{index}", table_id, f"Col{index}", "value", index),
            )
        for index in range(n_changes):
            db.execute(
                "INSERT INTO knowhow_changes (id,table_id,seq,kind,actor,origin,"
                "payload_json,fingerprint,note,created_at) VALUES "
                "(?,?,?,?,?,?,?,?,?,?)",
                (
                    f"{table_id}-chg{index}", table_id, index, "cell_edit", "u1",
                    "user", "{}", f"fp{index}", "", NOW,
                ),
            )


def test_delete_knowhow_tables_page_drains_each_child_table_in_bounded_sub_batches(
    repo,
):
    """计数桩断言:1 张表、7 列、7 条 changes、批大小 3——两张子表各自
    3+3+1=3 次 gate 调用（同 R8 的「探测式空批不问 gate」判据），
    knowhow_milestones 空,第一次探测就命中 rowcount==0,贡献 0 次；合计
    6 次。变异钉：把三张子表的删除折回父页单事务(去掉 gate 调用) →
    calls 变成 []，本条报红。"""
    _seed_user_and_notebook(repo)
    _seed_knowhow_table_with_columns_and_changes(repo, "nb1", "kt1", 7, 7)
    store = repo._runtime.notebook_delete_jobs
    store._CHILD_BATCH_SIZE = 3

    calls = []

    def counting_gate():
        calls.append(len(calls))
        return True

    count, last = store.delete_knowhow_tables_page(
        "nb1", "", 500, batch_ok=counting_gate,
    )
    assert count == 1
    assert last == "kt1"
    assert len(calls) == 7  # 6 次排水子批 + 父删前一问(R15)

    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowhow_tables WHERE notebook_id='nb1'"
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowhow_columns WHERE table_id='kt1'"
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowhow_changes WHERE table_id='kt1'"
        ).fetchone()["c"] == 0


def test_delete_knowhow_tables_page_gates_none_when_never_given_one(repo):
    """``batch_ok=None``(既有调用方/测试的默认值)必须继续一次不问地把整页
    删完——本参数存在之前的行为逐字不变。"""
    _seed_user_and_notebook(repo)
    _seed_knowhow_table_with_columns_and_changes(repo, "nb1", "kt1", 7, 7)
    store = repo._runtime.notebook_delete_jobs
    store._CHILD_BATCH_SIZE = 3

    count, last = store.delete_knowhow_tables_page("nb1", "", 500)
    assert count == 1
    assert last == "kt1"
    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowhow_tables WHERE notebook_id='nb1'"
        ).fetchone()["c"] == 0


def test_delete_knowhow_tables_page_stops_mid_drain_and_keeps_the_parent_row(
    repo,
):
    """gate 中途返回 False → 排水停手,父表 ``knowhow_tables`` 行**不删**
    （即便某张子表已经排空,只要还有别的子表没排完就不动父表行）;之后换
    恒真 gate 重跑收敛——不依赖任何持久化的子批游标。"""
    _seed_user_and_notebook(repo)
    _seed_knowhow_table_with_columns_and_changes(repo, "nb1", "kt1", 7, 7)
    store = repo._runtime.notebook_delete_jobs
    store._CHILD_BATCH_SIZE = 3

    calls = []

    def fails_on_second_call():
        calls.append(len(calls))
        return len(calls) < 2

    count, last = store.delete_knowhow_tables_page(
        "nb1", "", 500, batch_ok=fails_on_second_call,
    )
    assert count == 1  # page size (nonzero), NOT "chain drained"
    assert last is None  # caller's cursor = last or cursor is a no-op

    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowhow_tables WHERE notebook_id='nb1'"
        ).fetchone()["c"] == 1  # parent row untouched
        remaining_columns = db.execute(
            "SELECT COUNT(*) AS c FROM knowhow_columns WHERE table_id='kt1'"
        ).fetchone()["c"]
    assert remaining_columns == 1  # 7 - 3 - 3 (columns' own drain stopped here)

    count_again, last_again = store.delete_knowhow_tables_page(
        "nb1", "", 500, batch_ok=lambda: True,
    )
    assert count_again == 1
    assert last_again == "kt1"
    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowhow_tables WHERE notebook_id='nb1'"
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowhow_columns WHERE table_id='kt1'"
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowhow_changes WHERE table_id='kt1'"
        ).fetchone()["c"] == 0


def test_run_chain_does_not_delete_the_knowhow_table_when_the_sub_batch_gate_fails(
    repo,
):
    """端到端接线:``_run_chain`` 对 ``knowhow_tables`` 链同样构造
    ``sub_batch_ok``(复用 ``_batch_ok``)。claim 在第 2 个子批提交后失效 →
    排水中途停手、父表行未删、``_run_chain`` 返回 False。"""
    _seed_user_and_notebook(repo)
    _seed_knowhow_table_with_columns_and_changes(repo, "nb1", "kt1", 7, 7)
    store = repo._runtime.notebook_delete_jobs
    store._CHILD_BATCH_SIZE = 3

    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    lease_token = repo._runtime.notebook_delete_jobs.mark_running(
        job["id"], stale_cutoff_seconds=300,
    )
    runner = repo._runtime.notebook_delete

    class _ClaimThatFailsAfterTwoChecks:
        def __init__(self):
            self.checks = 0

        def verify_held(self):
            self.checks += 1
            return self.checks <= 2

        def release(self):
            pass

    claim = _ClaimThatFailsAfterTwoChecks()
    finished = runner._run_chain(
        job["id"], "nb1", lease_token, claim, "cursor_knowhow_tables",
        "knowhow_tables", "", residual=False,
    )
    assert finished is False

    row = repo._runtime.notebook_delete_jobs.get(job["id"])
    assert row["cursor_key"] == ""
    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowhow_tables WHERE notebook_id='nb1'"
        ).fetchone()["c"] == 1


# ---------------------------------------------------------------------------
# codex #659 R10 P1: delete_indexing_pipeline_stages_page's ctid/rowid-
# bounded drain of indexing_pipeline_stage_sources ran entirely INSIDE the
# one transaction the page method opened -- each DELETE statement was
# bounded, but the transaction's total row count across however many loop
# iterations it took was not. Same fix as R8/R9: drain via
# _drain_children_by_parent_ids (batch_ok-gated), only deleting the parent
# indexing_pipeline_stages rows once fully drained.
# ---------------------------------------------------------------------------


def _seed_indexing_pipeline_job_with_many_sources(repo, notebook_id, job_id, n_sources):
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO kg_build_jobs (id,notebook_id,created_by,mode,"
            "status,stage,total_sources,completed_sources,failed_sources,"
            "error_code,error_message,created_at,updated_at,finished_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, notebook_id, "u1", "incremental", "succeeded", "done",
             n_sources, n_sources, 0, "", "", NOW, NOW, NOW),
        )
        db.execute(
            "INSERT INTO indexing_pipeline_stages (job_id,notebook_id,"
            "pipeline_id,pipeline_version,pipeline_generation,"
            "source_snapshot,created_at,updated_at) VALUES "
            "(?,?,?,?,?,?,?,?)",
            (job_id, notebook_id, "builtin", "1", "g1", "[]", NOW, NOW),
        )
        for index in range(n_sources):
            source_id = f"{job_id}-src{index}"
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,file_path,"
                "source_type,status,parse_status,uploaded_by,created_at,"
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (source_id, notebook_id, "S", "", "pdf", "ready", "parsed",
                 "u1", NOW, NOW),
            )
            db.execute(
                "INSERT INTO indexing_pipeline_stage_sources (job_id,"
                "source_id,status,payload,created_at,updated_at) VALUES "
                "(?,?,?,?,?,?)",
                (job_id, source_id, "completed", "{}", NOW, NOW),
            )


def test_delete_indexing_pipeline_stages_page_drains_stage_sources_in_bounded_sub_batches(
    repo,
):
    """计数桩断言:1 个 job、7 条 stage_sources、批大小 3(3+3+1)→ gate 恰好
    3 次（同 R8/R9 的「探测式空批不问 gate」判据）。变异钉：把
    stage_sources 的删除折回父页单事务(去掉 batch_ok 门参调用) → calls 变成
    []，本条报红。"""
    _seed_user_and_notebook(repo)
    _seed_indexing_pipeline_job_with_many_sources(repo, "nb1", "kgj1", 7)
    store = repo._runtime.notebook_delete_jobs
    store._CHILD_BATCH_SIZE = 3

    calls = []

    def counting_gate():
        calls.append(len(calls))
        return True

    count, last = store.delete_indexing_pipeline_stages_page(
        "nb1", "", 500, batch_ok=counting_gate,
    )
    assert count == 1
    assert last == "kgj1"
    assert len(calls) == 4  # 3 次排水子批 + 父删前一问(R15)

    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM indexing_pipeline_stages "
            "WHERE notebook_id='nb1'"
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS c FROM indexing_pipeline_stage_sources "
            "WHERE job_id='kgj1'"
        ).fetchone()["c"] == 0


def test_delete_indexing_pipeline_stages_page_gates_none_when_never_given_one(repo):
    """``batch_ok=None``(既有调用方/测试的默认值)必须继续一次不问地把整页
    删完——本参数存在之前的行为逐字不变。"""
    _seed_user_and_notebook(repo)
    _seed_indexing_pipeline_job_with_many_sources(repo, "nb1", "kgj1", 7)
    store = repo._runtime.notebook_delete_jobs
    store._CHILD_BATCH_SIZE = 3

    count, last = store.delete_indexing_pipeline_stages_page("nb1", "", 500)
    assert count == 1
    assert last == "kgj1"
    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM indexing_pipeline_stages "
            "WHERE notebook_id='nb1'"
        ).fetchone()["c"] == 0


def test_delete_indexing_pipeline_stages_page_stops_mid_drain_and_keeps_the_parent_row(
    repo,
):
    """gate 中途返回 False → 排水停手,父行 ``indexing_pipeline_stages`` 不删；
    之后换恒真 gate 重跑收敛——不依赖任何持久化的子批游标。"""
    _seed_user_and_notebook(repo)
    _seed_indexing_pipeline_job_with_many_sources(repo, "nb1", "kgj1", 7)
    store = repo._runtime.notebook_delete_jobs
    store._CHILD_BATCH_SIZE = 3

    calls = []

    def fails_on_second_call():
        calls.append(len(calls))
        return len(calls) < 2

    count, last = store.delete_indexing_pipeline_stages_page(
        "nb1", "", 500, batch_ok=fails_on_second_call,
    )
    assert count == 1  # page size (nonzero), NOT "chain drained"
    assert last is None  # caller's cursor = last or cursor is a no-op

    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM indexing_pipeline_stages "
            "WHERE notebook_id='nb1'"
        ).fetchone()["c"] == 1  # parent row untouched
        remaining = db.execute(
            "SELECT COUNT(*) AS c FROM indexing_pipeline_stage_sources "
            "WHERE job_id='kgj1'"
        ).fetchone()["c"]
    assert remaining == 1  # 7 - 3 - 3 (first two sub-batches already committed)

    count_again, last_again = store.delete_indexing_pipeline_stages_page(
        "nb1", "", 500, batch_ok=lambda: True,
    )
    assert count_again == 1
    assert last_again == "kgj1"
    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM indexing_pipeline_stages "
            "WHERE notebook_id='nb1'"
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS c FROM indexing_pipeline_stage_sources "
            "WHERE job_id='kgj1'"
        ).fetchone()["c"] == 0


def test_run_chain_does_not_delete_the_indexing_pipeline_stage_when_the_sub_batch_gate_fails(
    repo,
):
    """端到端接线:``_run_chain`` 对 ``indexing_pipeline_stages`` 链同样构造
    ``sub_batch_ok``(复用 ``_batch_ok``)。claim 在第 2 个子批提交后失效 →
    排水中途停手、父行未删、``_run_chain`` 返回 False。"""
    _seed_user_and_notebook(repo)
    _seed_indexing_pipeline_job_with_many_sources(repo, "nb1", "kgj1", 7)
    store = repo._runtime.notebook_delete_jobs
    store._CHILD_BATCH_SIZE = 3

    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    lease_token = repo._runtime.notebook_delete_jobs.mark_running(
        job["id"], stale_cutoff_seconds=300,
    )
    runner = repo._runtime.notebook_delete

    class _ClaimThatFailsAfterTwoChecks:
        def __init__(self):
            self.checks = 0

        def verify_held(self):
            self.checks += 1
            return self.checks <= 2

        def release(self):
            pass

    claim = _ClaimThatFailsAfterTwoChecks()
    finished = runner._run_chain(
        job["id"], "nb1", lease_token, claim, "cursor_indexing_pipeline_stages",
        "indexing_pipeline_stages", "", residual=False,
    )
    assert finished is False

    row = repo._runtime.notebook_delete_jobs.get(job["id"])
    assert row["cursor_key"] == ""
    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM indexing_pipeline_stages "
            "WHERE notebook_id='nb1'"
        ).fetchone()["c"] == 1


# ---------------------------------------------------------------------------
# codex #659 R10 P1: delete_knowhow_rows_page's "N rows x that table's own
# column count" bound was never actually enforced by the app (no cap on a
# knowhow table's column count) -- a page's knowhow_cells/knowhow_cell_code
# DELETEs (single unbounded row_id = ANY(row_ids) statements) could still be
# unbounded, all inside the one transaction the page method used to open.
# Same fix: drain via _drain_children_by_parent_ids (batch_ok-gated), only
# deleting the parent knowhow_rows rows once fully drained.
# ---------------------------------------------------------------------------


def _seed_knowhow_row_with_many_cells(repo, notebook_id, table_id, row_id, n_columns):
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO knowhow_tables (id,notebook_id,title,description,"
            "mutation_seq,created_by,created_at,updated_at) VALUES "
            "(?,?,?,?,?,?,?,?)",
            (table_id, notebook_id, "KT", "", 0, "u1", NOW, NOW),
        )
        db.execute(
            "INSERT INTO knowhow_rows (id,table_id,position,"
            "projection_status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
            (row_id, table_id, 0, "none", NOW, NOW),
        )
        for index in range(n_columns):
            column_id = f"{table_id}-col{index}"
            db.execute(
                "INSERT INTO knowhow_columns (id,table_id,name,role,position) "
                "VALUES (?,?,?,?,?)",
                (column_id, table_id, f"Col{index}", "value", index),
            )
            db.execute(
                "INSERT INTO knowhow_cells (id,row_id,column_id,content_md,"
                "updated_at) VALUES (?,?,?,?,?)",
                (f"{row_id}-cell{index}", row_id, column_id, "v", NOW),
            )
            db.execute(
                "INSERT INTO knowhow_cell_code (id,row_id,column_id,code_text,"
                "language,updated_by,cell_content_hash,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (f"{row_id}-code{index}", row_id, column_id, "print(1)",
                 "python", "u1", f"hash{index}", NOW, NOW),
            )


def test_delete_knowhow_rows_page_drains_cells_in_bounded_sub_batches(repo):
    """计数桩断言:1 行、7 列(→7 个 cells + 7 个 cell_code)、批大小 3
    (3+3+1)→ 两张子表各 3 次,合计 6 次（同 R8/R9/R10 的「探测式空批不问
    gate」判据）。变异钉：把 cells/cell_code 的删除折回父页单事务(去掉
    batch_ok 门参调用) → calls 变成 []，本条报红。"""
    _seed_user_and_notebook(repo)
    _seed_knowhow_row_with_many_cells(repo, "nb1", "kt1", "kr1", 7)
    store = repo._runtime.notebook_delete_jobs
    store._CHILD_BATCH_SIZE = 3

    calls = []

    def counting_gate():
        calls.append(len(calls))
        return True

    count, last = store.delete_knowhow_rows_page(
        "nb1", "", 500, batch_ok=counting_gate,
    )
    assert count == 1
    assert last == "kr1"
    assert len(calls) == 7  # 6 次排水子批 + 父删前一问(R15)

    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowhow_rows WHERE table_id='kt1'"
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowhow_cells WHERE row_id='kr1'"
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowhow_cell_code WHERE row_id='kr1'"
        ).fetchone()["c"] == 0


def test_delete_knowhow_rows_page_gates_none_when_never_given_one(repo):
    """``batch_ok=None``(既有调用方/测试的默认值)必须继续一次不问地把整页
    删完——本参数存在之前的行为逐字不变。"""
    _seed_user_and_notebook(repo)
    _seed_knowhow_row_with_many_cells(repo, "nb1", "kt1", "kr1", 7)
    store = repo._runtime.notebook_delete_jobs
    store._CHILD_BATCH_SIZE = 3

    count, last = store.delete_knowhow_rows_page("nb1", "", 500)
    assert count == 1
    assert last == "kr1"
    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowhow_rows WHERE table_id='kt1'"
        ).fetchone()["c"] == 0


def test_delete_knowhow_rows_page_stops_mid_drain_and_keeps_the_parent_row(repo):
    """gate 中途返回 False → 排水停手,父行 ``knowhow_rows`` 不删;之后换
    恒真 gate 重跑收敛——不依赖任何持久化的子批游标。"""
    _seed_user_and_notebook(repo)
    _seed_knowhow_row_with_many_cells(repo, "nb1", "kt1", "kr1", 7)
    store = repo._runtime.notebook_delete_jobs
    store._CHILD_BATCH_SIZE = 3

    calls = []

    def fails_on_second_call():
        calls.append(len(calls))
        return len(calls) < 2

    count, last = store.delete_knowhow_rows_page(
        "nb1", "", 500, batch_ok=fails_on_second_call,
    )
    assert count == 1  # page size (nonzero), NOT "chain drained"
    assert last is None  # caller's cursor = last or cursor is a no-op

    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowhow_rows WHERE table_id='kt1'"
        ).fetchone()["c"] == 1  # parent row untouched
        remaining = db.execute(
            "SELECT COUNT(*) AS c FROM knowhow_cells WHERE row_id='kr1'"
        ).fetchone()["c"]
    assert remaining == 1  # 7 - 3 - 3 (first two sub-batches already committed)

    count_again, last_again = store.delete_knowhow_rows_page(
        "nb1", "", 500, batch_ok=lambda: True,
    )
    assert count_again == 1
    assert last_again == "kr1"
    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowhow_rows WHERE table_id='kt1'"
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowhow_cells WHERE row_id='kr1'"
        ).fetchone()["c"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowhow_cell_code WHERE row_id='kr1'"
        ).fetchone()["c"] == 0


def test_run_chain_does_not_delete_the_knowhow_row_when_the_sub_batch_gate_fails(repo):
    """端到端接线:``_run_chain`` 对 ``knowhow_rows`` 链同样构造
    ``sub_batch_ok``(复用 ``_batch_ok``)。claim 在第 2 个子批提交后失效 →
    排水中途停手、父行未删、``_run_chain`` 返回 False。"""
    _seed_user_and_notebook(repo)
    _seed_knowhow_row_with_many_cells(repo, "nb1", "kt1", "kr1", 7)
    store = repo._runtime.notebook_delete_jobs
    store._CHILD_BATCH_SIZE = 3

    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    lease_token = repo._runtime.notebook_delete_jobs.mark_running(
        job["id"], stale_cutoff_seconds=300,
    )
    runner = repo._runtime.notebook_delete

    class _ClaimThatFailsAfterTwoChecks:
        def __init__(self):
            self.checks = 0

        def verify_held(self):
            self.checks += 1
            return self.checks <= 2

        def release(self):
            pass

    claim = _ClaimThatFailsAfterTwoChecks()
    finished = runner._run_chain(
        job["id"], "nb1", lease_token, claim, "cursor_knowhow_rows",
        "knowhow_rows", "", residual=False,
    )
    assert finished is False

    row = repo._runtime.notebook_delete_jobs.get(job["id"])
    assert row["cursor_key"] == ""
    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowhow_rows WHERE table_id='kt1'"
        ).fetchone()["c"] == 1


def test_parent_delete_is_gated_after_the_child_drain_finishes(repo):
    """codex #659 R15：子表排水完成之后、父行删除事务之前必须再问一次
    gate——排水可能长到租/claim 被偷,父删是独立的毁灭性写事务。构造:gate
    在全部排水子批放行,排水结束后的下一问返回 False→父行(knowhow_rows)
    必须原样保留,cells 已排空(排水本身合法),返回 (count,None) 游标不前
    进。变异钉:去掉父删前的 gate 调用→本条红(父行被删)。"""
    _seed_user_and_notebook(repo)
    _seed_knowhow_row_with_many_cells(repo, "nb1", "kt1", "kr1", 7)
    store = repo._runtime.notebook_delete_jobs
    store._CHILD_BATCH_SIZE = 3

    calls = []

    def gate():
        calls.append(len(calls))
        return len(calls) <= 6  # 6 次排水子批放行,第 7 问(父删前)拒绝

    count, last = store.delete_knowhow_rows_page(
        "nb1", "", 500, batch_ok=gate,
    )
    assert count == 1
    assert last is None, "gate 拦下父删时游标不得前进"
    assert len(calls) == 7

    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowhow_rows WHERE id='kr1'"
        ).fetchone()["c"] == 1, (
            "the parent row must survive when ownership is lost after the drain"
        )
        assert db.execute(
            "SELECT COUNT(*) AS c FROM knowhow_cells WHERE row_id='kr1'"
        ).fetchone()["c"] == 0


# ---------------------------------------------------------------------------
# codex #659 R17 P2-a：资产写后复核必须是「生命周期探针」，不是授权目录视图
# ---------------------------------------------------------------------------


def test_asset_recheck_survives_actor_access_loss(repo, monkeypatch):
    """变异钉：把 ``_default_notebook_exists`` 退回「先问 get_notebook」→
    本用例模拟请求方在插行与复核之间失去读权限(分享被撤)——get_notebook
    抛 KeyError,但笔记本本体活得好好的;旧探针会把刚写的合法文件补偿删掉,
    留下一条指向缺失内容的 notebook_assets 行,报红。"""
    from app.services.knowhow.assets import AssetService

    _seed_user_and_notebook(repo)

    def _actor_lost_access(notebook_id, *args, **kwargs):
        raise KeyError(notebook_id)

    monkeypatch.setattr(repo, "get_notebook", _actor_lost_access, raising=False)

    service = AssetService(repo)
    asset = service.save("nb1", "cell.png", "image/png", b"\x89PNG fake", "u1")
    path = service.path_for(asset)
    assert path.is_file(), "笔记本仍在,权限位波动不得触发补偿删除"
    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS c FROM notebook_assets WHERE id=?",
            (asset["id"],),
        ).fetchone()["c"] == 1


def test_asset_recheck_still_compensates_when_notebook_is_deleting(repo):
    """R17 改探针后另一腿不得松动：status='deleting'（墓碑已落）时写后复核
    必须补偿删除刚写的文件并抛 KeyError——与 R6 的补偿契约逐字一致。"""
    from app.services.knowhow.assets import AssetService

    _seed_user_and_notebook(repo, status="deleting")

    service = AssetService(repo)
    with pytest.raises(KeyError):
        service.save("nb1", "cell.png", "image/png", b"\x89PNG fake", "u1")
    nb_asset_dir = Path(repo.storage_dir) / "assets" / "nb1"
    leftovers = list(nb_asset_dir.glob("*")) if nb_asset_dir.exists() else []
    assert leftovers == [], f"deleting 状态下写后复核应补偿删除,残留：{leftovers}"


# ---------------------------------------------------------------------------
# codex #659 R17 P2-b：歧义提交失败(COMMIT 已生效、确认丢失)不得删已提交行的文件
# ---------------------------------------------------------------------------


def test_upload_keeps_file_when_commit_succeeded_but_ack_was_lost(
    repo, monkeypatch,
):
    """变异钉：把 except 分支的 ``source_exists`` 核对拆掉、退回无条件删文件 →
    本用例模拟 PG 上 COMMIT 已在服务端生效、确认包丢失(客户端拿到异常)的
    歧义失败:行在库里、文件却被删,产生一条指向缺失内容的 sources 行,报红。"""
    _seed_user_and_notebook(repo)
    ingestion = repo._runtime.source_ingestion
    from app.services.source_ingestion import UploadedSourceFile

    real_insert = ingestion._insert_uploaded_source

    def _commit_then_lose_ack(*args, **kwargs):
        real_insert(*args, **kwargs)  # 行真实提交
        raise RuntimeError("connection dropped after commit (simulated)")

    monkeypatch.setattr(ingestion, "_insert_uploaded_source", _commit_then_lose_ack)

    with pytest.raises(RuntimeError):
        ingestion.upload_sources(
            "nb1",
            [UploadedSourceFile(
                file_name="doc.pdf", content_type="application/pdf",
                content=b"hello ambiguous commit",
            )],
            None, ingestion.pipeline_hooks(),
        )

    with repo._runtime.database.connect() as db:
        row = db.execute(
            "SELECT id FROM sources WHERE notebook_id='nb1'"
        ).fetchone()
    assert row is not None, "前置不成立:模拟的提交没落库"
    storage_dir = repo._runtime.notebook_delete._storage_dir()
    source_dir = storage_dir / "notebooks" / "nb1"
    leftover = list(source_dir.iterdir()) if source_dir.exists() else []
    assert leftover != [], "行已提交(歧义失败实为成功)时不得删它的文件"


# ---------------------------------------------------------------------------
# codex #659 R18 P1：作业化 finalize 也要无条件清 FTS5 影子——墓碑前放行的
# 解析/重解析不受任何 quiesce 腿约束,可以在相位 3 的 FTS 清扫之后、finalize
# 之前提交 chunks/KG 行;级联删得掉真身,影子表却是手工维护的。
# ---------------------------------------------------------------------------


def test_jobized_finalize_resweeps_late_committed_fts_shadows(repo):
    """变异钉:把 finalize 里的两条 FTS 影子 DELETE 加回 ``if job_id is
    None:`` 守卫 → 本用例模拟相位 3 之后才提交的迟到影子行,作业化收尾后
    全文永久残留,报红。同时钉住别的库的影子行原样。"""
    _seed_user_and_notebook(repo)
    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    lease_token = repo._runtime.notebook_delete_jobs.mark_running(
        job["id"], stale_cutoff_seconds=300,
    )
    # 相位 3 已经跑完(这里没跑,等价于清扫过后表为空),然后一个墓碑前放行的
    # 重解析迟到提交了影子行:
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO chunks_fts (chunk_id,notebook_id,text) "
            "VALUES ('late-ch','nb1','late committed chunk text')",
        )
        db.execute(
            "INSERT INTO kg_objects_fts (object_id,notebook_id,name) "
            "VALUES ('late-kg','nb1','late committed object')",
        )
        db.execute(
            "INSERT INTO chunks_fts (chunk_id,notebook_id,text) "
            "VALUES ('other-ch','nb2','keep me')",
        )

    repo._runtime.notebook_store.delete_row_and_orphan_embeddings(
        "nb1", job_id=job["id"], lease_token=lease_token,
    )

    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) c FROM chunks_fts WHERE notebook_id='nb1'"
        ).fetchone()["c"] == 0, "迟到提交的 chunks_fts 影子必须被 finalize 清掉"
        assert db.execute(
            "SELECT COUNT(*) c FROM kg_objects_fts WHERE notebook_id='nb1'"
        ).fetchone()["c"] == 0, "迟到提交的 kg_objects_fts 影子必须被 finalize 清掉"
        assert db.execute(
            "SELECT COUNT(*) c FROM chunks_fts WHERE notebook_id='nb2'"
        ).fetchone()["c"] == 1, "别的库的影子行不许连坐"


# ---------------------------------------------------------------------------
# codex #659 R18 P2：残渣收尾路径 settle 之后也要涂抹分析产物——它们不在
# notebooks 级联里,settle 掉作业却不涂抹意味着永远没人再来清。
# ---------------------------------------------------------------------------


def test_residual_cleanup_redacts_analysis_artifacts(repo, monkeypatch):
    """变异钉:把 ``_finish_residual`` 里的 ``_redact_analysis_artifacts``
    调用删掉 → 带外删除场景下 redact_notebook 一次都不会被叫到,报红。"""
    _seed_user_and_notebook(repo)
    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    with repo._runtime.database.write() as db:
        db.execute("DELETE FROM notebooks WHERE id='nb1'")  # out-of-band delete

    runner = repo._runtime.notebook_delete
    redacted: list[str] = []

    class _RecordingArtifacts:
        def redact_notebook(self, notebook_id, *, occurred_at):
            redacted.append(notebook_id)

    monkeypatch.setattr(runner, "_analysis_artifacts", _RecordingArtifacts())
    runner.run(job["id"])

    assert redacted == ["nb1"], (
        "残渣收尾 settle 后必须涂抹分析产物,实际调用记录:" f"{redacted}"
    )
    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) c FROM notebook_delete_jobs"
        ).fetchone()["c"] == 0, "前置不成立:残渣收尾没有 settle 作业行"


# ---------------------------------------------------------------------------
# codex #659 R19 P2：相位 1 路径物化也要租约围栏——一页拷贝超过 sweep 窗口时
# 租约可能被偷走,旧 worker 继续写 notebook_delete_files 会与新主人读到同一个
# MAX(ordinal),主键相撞把新主人的删除打成 failed。
# ---------------------------------------------------------------------------


def test_paths_materialization_is_lease_fenced(repo):
    """变异钉:把 materialize_paths_page 开头的事务内 lease CAS 拆掉 →
    被偷走租约的旧 worker 照样写入路径行,报红。同时钉住:_phase_paths 对
    围栏未命中返回 False(调用方就地停手,不推进相位)。"""
    _seed_user_and_notebook(repo)
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,file_path,source_type,"
            "status,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("s1", "nb1", "s1", "/tmp/a.pdf", "pdf", "ready", "parsed", NOW, NOW),
        )
    store = repo._runtime.notebook_delete_jobs
    job = store.request("nb1", "u1")
    stale_lease = store.mark_running(job["id"], stale_cutoff_seconds=300)
    # 模拟 sweep 判旧 worker 死亡后偷走租约:把心跳倒拨一小时(等价于一页
    # 拷贝卡了很久),再按正常 cutoff 偷。
    from datetime import datetime, timedelta

    past = (datetime.now().astimezone() - timedelta(hours=1)).isoformat()
    with repo._runtime.database.write() as db:
        db.execute(
            "UPDATE notebook_delete_jobs SET updated_at=? WHERE id=?",
            (past, job["id"]),
        )
    fresh_lease = store.mark_running(job["id"], stale_cutoff_seconds=300)
    assert fresh_lease is not None and fresh_lease != stale_lease, (
        "前置不成立:租约没有被偷走"
    )

    # 旧租约的写必须被围栏挡下:返回 None,一行都不落
    assert store.materialize_paths_page(
        job["id"], "nb1", "", 500, lease_token=stale_lease,
    ) is None
    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) c FROM notebook_delete_files"
        ).fetchone()["c"] == 0, "围栏未命中时不得写入任何路径行"

    # 服务层对围栏未命中就地停手,不把相位推进到 'paths'
    runner = repo._runtime.notebook_delete
    assert runner._phase_paths(job["id"], "nb1", stale_lease, "") is False
    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT phase FROM notebook_delete_jobs WHERE id=?", (job["id"],)
        ).fetchone()["phase"] == "mark"

    # 新主人照常干活,且围栏 CAS 顺带刷了心跳
    result = store.materialize_paths_page(
        job["id"], "nb1", "", 500, lease_token=fresh_lease,
    )
    assert result == (1, "s1")


# ---------------------------------------------------------------------------
# codex #659 R20 P1：分析产物的两条非模型发布路径（表格清单/隔离副本）也要
# 生命周期闸——墓碑前放行的解析/编译在相位 5 一次性涂抹之后收尾时,不得把
# 已删笔记本的内容重新发布回磁盘（作业已经没了,永远没人再来清）。
# ---------------------------------------------------------------------------


def test_analysis_artifact_publication_refuses_deleting_notebook(repo, tmp_path):
    """变异钉:把 save_spreadsheet_manifest / record_issue 里的
    _publication_admitted 闸拆掉 → deleting 笔记本的清单/隔离副本照样落盘,
    报红。同钉:活笔记本两条路径照常发布(闸不是一刀切拒绝)。"""
    _seed_user_and_notebook(repo)
    _seed_user_and_notebook(repo, notebook_id="nb-dying", owner="u2", status="deleting")
    artifacts = repo._runtime.analysis_artifacts

    quarantine = tmp_path / "quarantine.csv"
    quarantine.write_text("secret,data\n1,2\n", encoding="utf-8")

    # deleting 笔记本:两条路径都拒绝,磁盘零痕迹
    artifacts.save_spreadsheet_manifest("nb-dying", "s1", {"sheets": []})
    assert artifacts.load_spreadsheet_manifest("nb-dying", "s1") is None, (
        "deleting 笔记本的清单不得落盘"
    )
    refused = artifacts.record_issue(
        notebook_id="nb-dying", notebook_name="NB", owner_id="u2",
        source_id="s1", source_title="T", file_name="a.csv",
        source_type="csv", category="source_parse", code="X",
        summary="s", occurred_at=NOW, source_path=str(quarantine),
    )
    assert refused == {}, "拒绝时应返回 {} (与 record_model_output_issue 同形)"
    issue_dir = artifacts.root / "issues" / "nb-dying"
    assert not issue_dir.exists(), "deleting 笔记本的隔离副本不得落盘"

    # 活笔记本:两条路径照常发布
    artifacts.save_spreadsheet_manifest("nb1", "s1", {"sheets": ["ok"]})
    assert artifacts.load_spreadsheet_manifest("nb1", "s1") == {"sheets": ["ok"]}
    issue = artifacts.record_issue(
        notebook_id="nb1", notebook_name="NB", owner_id="u1",
        source_id="s1", source_title="T", file_name="a.csv",
        source_type="csv", category="source_parse", code="X",
        summary="s", occurred_at=NOW, source_path=str(quarantine),
    )
    assert issue and issue["notebook_id"] == "nb1"
    assert (artifacts.root / "issues" / "nb1" / "s1" / "source_parse" / "payload").is_file()
