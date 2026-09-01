"""Batch 3·W1 PR-3 Phase B — internal code-review follow-up fixes (P1-A
residual cleanup, P1-B claim spanning phases 3-5 + phase-3 verify_held,
P1-E failed-job backoff/ceiling, P2-b closure reconciliation). SQLite lane;
the PostgreSQL-only additions live in the existing
``tests/postgres/test_notebook_delete_jobization_pg.py`` and
``tests/postgres/test_notebook_delete_rows_and_files_pg.py``.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
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
    DELETE 语句都还在——结构性测试，不依赖最终状态。"""
    import inspect

    from app.repositories.postgres.notebook_delete_job_store import (
        NotebookDeleteJobStore as PgStore,
    )
    from app.repositories.sqlite.notebook_delete_job_store import (
        NotebookDeleteJobStore as SqliteStore,
    )

    expectations = {
        "delete_knowhow_rows_page": ("knowhow_cell_code",),
        "delete_knowhow_tables_page": ("knowhow_milestones",),
        "delete_memory_items_page": ("memory_revisions",),
        "delete_indexing_pipeline_stages_page": (
            "indexing_pipeline_stages", "indexing_pipeline_stage_sources",
        ),
    }
    for store_cls in (PgStore, SqliteStore):
        for method_name, required_tables in expectations.items():
            source = inspect.getsource(getattr(store_cls, method_name))
            for table in required_tables:
                assert f"FROM {table}" in source, (
                    f"{store_cls.__name__}.{method_name} no longer issues an "
                    f"explicit DELETE FROM {table} (P2-d)"
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
    repo._runtime.notebook_delete_jobs.mark_running(job["id"], stale_cutoff_seconds=300)
    repo._runtime.notebook_delete_jobs.finish(
        job["id"], "failed", error_code="x", error_message="y",
    )
    row = repo._runtime.notebook_delete_jobs.get(job["id"])
    assert row["attempts"] == 1


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
