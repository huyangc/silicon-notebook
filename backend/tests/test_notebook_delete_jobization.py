"""Batch 3·W1 PR-3 Phase A: tombstone + phases 0/1/2/5 of the six-phase
delete job (design doc §T-2/§T-3/§T-4). SQLite lane — see
``tests/postgres/test_notebook_delete_jobization_pg.py`` for the PostgreSQL
equivalents this file cannot exercise (real EXPLAIN, real advisory locks).

Covers:
  1. Tombstone CAS: single-row UPDATE, 404 (missing) vs 409 (already
     copying/deleting) distinguished (§T-2).
  2. Quiesce dual leg (§T-3.3): leg A (durable kg_build_jobs), leg B
     (in-process KgMaintenanceJobs) — timeout leaves the job 'waiting', never
     forces phase 3.
  3. The three in-flight-rebuild checkpoints (§4.2 option A): buildkg-'s
     batch loop, relinkkg-'s source loop, unifiedkg-'s _stage.
  4. Phase 5 finalize: single-transaction atomicity, archive-output
     equivalence with the legacy synchronous path (G1 hard gate), job/side-
     table cleanup in both the normal path and the "notebook row already
     gone" early-return branch.
  5. Sweep's two drivers (§T-4): stale active job rows, and a 'deleting'
     notebook missing its job row.
  6. API 202 contract + frontend zero-change anchor.
  7. Startup wiring: the periodic sweeper starts with run_startup() and
     stops with close_repository(), same lease-scoped lifecycle as the
     extension-admission refresher it mirrors.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.repositories.ports import NotebookAlreadyDeletingError
from app.services import background_jobs
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
        existing = db.execute(
            "SELECT 1 FROM users WHERE id=?", (owner,)
        ).fetchone()
        if existing is None:
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


def _seed_source(repo, notebook_id, source_id, file_path=""):
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,file_path,source_type,"
            "status,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, source_id, file_path, "pdf", "ready",
             "parsed", NOW, NOW),
        )


def _drain():
    background_jobs._drain_maintenance_executors_for_tests(timeout=10.0)


# ---------------------------------------------------------------------------
# 1. Tombstone CAS (§T-2)
# ---------------------------------------------------------------------------


def test_request_cas_missing_notebook_raises_keyerror(repo):
    with pytest.raises(KeyError):
        repo._runtime.notebook_delete_jobs.request("nb-does-not-exist", "u1")


def test_request_cas_already_deleting_raises_conflict(repo):
    _seed_user_and_notebook(repo)
    repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    with pytest.raises(NotebookAlreadyDeletingError):
        repo._runtime.notebook_delete_jobs.request("nb1", "u1")


def test_request_cas_already_copying_raises_conflict(repo):
    _seed_user_and_notebook(repo, status="copying")
    with pytest.raises(NotebookAlreadyDeletingError):
        repo._runtime.notebook_delete_jobs.request("nb1", "u1")


def test_request_cas_is_a_single_row_update_and_creates_one_job_row(repo):
    """变异钉:把 CAS 谓词从 NOTEBOOK_LIVE_SQL 换回裸 `status='ready'` 之类的
    等值形会让这条用例仍然通过(误报绿),但把它换成一个永假谓词(例如
    `status='never'`)会让 request() 恒抛 KeyError——本用例的存在价值是钉住
    「CAS 之后 notebooks.status 变成 deleting 且恰好一行作业」这件事本身。"""
    _seed_user_and_notebook(repo)
    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    assert job["status"] == "queued"
    assert job["phase"] == "mark"
    with repo._runtime.database.connect() as conn:
        status = conn.execute(
            "SELECT status FROM notebooks WHERE id='nb1'"
        ).fetchone()["status"]
        job_count = conn.execute(
            "SELECT COUNT(*) AS c FROM notebook_delete_jobs WHERE notebook_id='nb1'"
        ).fetchone()["c"]
    assert status == "deleting"
    assert job_count == 1


def test_deleting_notebook_is_immediately_invisible_via_the_api(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app

    client = TestClient(app)
    client.post("/api/auth/register", json={"username": "d00000001", "password": "pw123456"})
    headers = {
        "Authorization": "Bearer "
        + client.post(
            "/api/auth/login", json={"username": "d00000001", "password": "pw123456"}
        ).json()["token"]
    }
    nb = client.post("/api/notebooks", json={"name": "D"}, headers=headers).json()["id"]

    resp = client.delete(f"/api/notebooks/{nb}", headers=headers)
    assert resp.status_code == 202
    assert resp.json() == {"status": "deleting"}
    # T-1's NOTEBOOK_LIVE_SQL filter hides it immediately — before the
    # background job has necessarily finished (§5: "deleting 的库经 T-1
    # 统一谓词一律不可见").
    assert client.get(f"/api/notebooks/{nb}", headers=headers).status_code == 404

    _drain()


def test_second_sequential_delete_request_gets_409_from_the_owner_scoped_guard(
    tmp_path, monkeypatch,
):
    """codex #659 R6 P2:一次 CAS 提交后,同一个 owner 串行发第二次 DELETE 必须
    拿到文档承诺的 409,不是 404。

    在这条修复之前,`require_notebook_capability("notebook:delete")`(owner
    档 → `require_notebook_write`)走的是带生命周期过滤的 `NOTEBOOK_WRITE_SQL`
    (`AND NOTEBOOK_LIVE_SQL`)——第一次 CAS 一旦把 notebooks.status 翻成
    'deleting',这个笔记本对**任何**后续请求(包括第二次 DELETE 本身)在能力
    守卫这一层就已经"不存在",请求根本到不了路由处理函数里的
    `NotebookAlreadyDeletingError` → 409 分支。现在 DELETE 端点改用
    `require_notebook_delete`(裸 owner 判定,不带生命周期过滤),owner 对
    自己正在删除中的库仍然"看得见"这一行,请求能真正走到路由体内的 409
    分支。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app
    from app.api import deps

    client = TestClient(app)
    client.post("/api/auth/register", json={"username": "d00000002", "password": "pw123456"})
    headers = {
        "Authorization": "Bearer "
        + client.post(
            "/api/auth/login", json={"username": "d00000002", "password": "pw123456"}
        ).json()["token"]
    }
    nb = client.post("/api/notebooks", json={"name": "D2"}, headers=headers).json()["id"]

    rt = deps.repository()._runtime
    rt.notebook_delete._quiesce_timeout_seconds = 5.0
    with rt.database.write() as db:
        db.execute(
            "INSERT INTO kg_build_jobs (id,notebook_id,created_by,mode,status,"
            "stage,total_sources,completed_sources,failed_sources,error_code,"
            "error_message,created_at,updated_at,finished_at) VALUES "
            "('kgj-block',?,?,?,'running','extracting',1,0,0,'','',?,?,'')",
            (nb, "u", "incremental", NOW, NOW),
        )

    first = client.delete(f"/api/notebooks/{nb}", headers=headers)
    assert first.status_code == 202
    second = client.delete(f"/api/notebooks/{nb}", headers=headers)
    assert second.status_code == 409

    with rt.database.connect() as conn:
        job_count = conn.execute(
            "SELECT COUNT(*) AS c FROM notebook_delete_jobs WHERE notebook_id=?",
            (nb,),
        ).fetchone()["c"]
    assert job_count == 1  # 第二次请求没有悄悄再排一个作业

    with rt.database.write() as db:
        db.execute("UPDATE kg_build_jobs SET status='succeeded' WHERE id='kgj-block'")
    _drain()


def test_delete_by_a_non_owner_still_gets_404(tmp_path, monkeypatch):
    """codex #659 R6 P2 锚点:`require_notebook_delete` 放宽的**仅仅**是
    「owner 对自己正在删除/拷贝中的库仍然可见」——非 owner 对一本活着的库
    发 DELETE 必须仍是 404(不泄露存在性),与放宽前逐字相同。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app

    client = TestClient(app)
    client.post("/api/auth/register", json={"username": "d00000003", "password": "pw123456"})
    owner_headers = {
        "Authorization": "Bearer "
        + client.post(
            "/api/auth/login", json={"username": "d00000003", "password": "pw123456"}
        ).json()["token"]
    }
    client.post("/api/auth/register", json={"username": "d00000004", "password": "pw123456"})
    other_headers = {
        "Authorization": "Bearer "
        + client.post(
            "/api/auth/login", json={"username": "d00000004", "password": "pw123456"}
        ).json()["token"]
    }
    nb = client.post(
        "/api/notebooks", json={"name": "D3"}, headers=owner_headers
    ).json()["id"]

    resp = client.delete(f"/api/notebooks/{nb}", headers=other_headers)
    assert resp.status_code == 404

    # owner 自己仍然能正常删——非 owner 的探测没有意外把库本身弄坏。
    own = client.delete(f"/api/notebooks/{nb}", headers=owner_headers)
    assert own.status_code == 202
    _drain()


def test_non_delete_write_endpoint_still_404s_a_deleting_notebook(tmp_path, monkeypatch):
    """codex #659 R6 P2 锚点(其它端点的守卫零变化):`require_notebook_delete`
    只挂在 DELETE 端点上;同一个 owner 对一本已经在 'deleting' 的库发别的写
    请求(这里用 PATCH 改名)必须仍是 404——不能因为这次修复顺带放宽了别的
    端点。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app

    client = TestClient(app)
    client.post("/api/auth/register", json={"username": "d00000005", "password": "pw123456"})
    headers = {
        "Authorization": "Bearer "
        + client.post(
            "/api/auth/login", json={"username": "d00000005", "password": "pw123456"}
        ).json()["token"]
    }
    nb = client.post("/api/notebooks", json={"name": "D4"}, headers=headers).json()["id"]

    first = client.delete(f"/api/notebooks/{nb}", headers=headers)
    assert first.status_code == 202

    patch = client.patch(
        f"/api/notebooks/{nb}", json={"name": "renamed"}, headers=headers
    )
    assert patch.status_code == 404
    _drain()


# ---------------------------------------------------------------------------
# 2. Quiesce dual leg (§T-3.3)
# ---------------------------------------------------------------------------


def test_quiesce_leg_a_durable_kg_build_job_blocks_then_clears(repo):
    _seed_user_and_notebook(repo)
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO kg_build_jobs (id,notebook_id,created_by,mode,status,"
            "stage,total_sources,completed_sources,failed_sources,error_code,"
            "error_message,created_at,updated_at,finished_at) VALUES "
            "('kgj1','nb1','u1','incremental','running','extracting',1,0,0,"
            "'','',?,?,'')",
            (NOW, NOW),
        )
    runner = repo._runtime.notebook_delete
    runner._quiesce_timeout_seconds = 0.3
    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")

    runner.run(job["id"])
    waiting = repo._runtime.notebook_delete_jobs.get(job["id"])
    assert waiting["status"] == "waiting"
    assert "durable(kg_build_jobs)" in waiting["error_message"]

    with repo._runtime.database.write() as db:
        db.execute("UPDATE kg_build_jobs SET status='succeeded' WHERE id='kgj1'")
    runner._quiesce_timeout_seconds = 30
    runner.run(job["id"])
    with pytest.raises(KeyError):
        repo._runtime.notebook_delete_jobs.get(job["id"])  # finalize deleted it
    with repo._runtime.database.connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM notebooks WHERE id='nb1'").fetchone()
    assert row["c"] == 0


def test_quiesce_leg_b_in_process_kg_maintenance_blocks_then_clears(repo):
    """变异钉(design doc 明文点名的 v3 漏洞):去掉腿 B 会让本用例变红——
    relinkkg-/unifiedkg- 走纯进程内的 KgMaintenanceJobs 字典,一行
    kg_build_jobs 都不写,腿 A 单独查不到它。"""
    _seed_user_and_notebook(repo)
    lifecycle = repo._runtime.knowledge_lifecycle
    lifecycle.kg_maintenance.jobs["nb1"] = {
        "job_id": "rlj-fake", "notebook_id": "nb1", "kind": "relink",
        "status": "running", **dict(lifecycle.kg_maintenance.RELINK_COUNTERS),
    }

    runner = repo._runtime.notebook_delete
    runner._quiesce_timeout_seconds = 0.3
    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    runner.run(job["id"])

    waiting = repo._runtime.notebook_delete_jobs.get(job["id"])
    assert waiting["status"] == "waiting"
    assert "in-process(kg_maintenance)" in waiting["error_message"]

    lifecycle.kg_maintenance.jobs["nb1"]["status"] = "succeeded"
    runner._quiesce_timeout_seconds = 30
    runner.run(job["id"])
    with pytest.raises(KeyError):
        repo._runtime.notebook_delete_jobs.get(job["id"])


def test_quiesce_leg_b_covers_the_conflict_detection_registry_too(repo):
    """`kg_conflict_jobs` 是与 `kg_maintenance` 分开的第二个 `KgMaintenanceJobs`
    实例(conflictresolve- 独立槽)。腿 B 必须两个都查。"""
    _seed_user_and_notebook(repo)
    lifecycle = repo._runtime.knowledge_lifecycle
    lifecycle.kg_conflict_jobs.jobs["nb1"] = {
        "job_id": "cfj-fake", "notebook_id": "nb1", "kind": "conflict",
        "status": "running", **dict(lifecycle.kg_conflict_jobs.CONFLICT_COUNTERS),
    }

    runner = repo._runtime.notebook_delete
    runner._quiesce_timeout_seconds = 0.3
    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    runner.run(job["id"])

    waiting = repo._runtime.notebook_delete_jobs.get(job["id"])
    assert waiting["status"] == "waiting"


def test_quiesce_never_forces_phase_3_on_timeout(repo):
    """超时必须落 waiting、绝不静默推进相位——phase 停在 'paths'(相位 1 已完成,
    相位 2 未完成),不是任何更靠后的值。"""
    _seed_user_and_notebook(repo)
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO kg_build_jobs (id,notebook_id,created_by,mode,status,"
            "stage,total_sources,completed_sources,failed_sources,error_code,"
            "error_message,created_at,updated_at,finished_at) VALUES "
            "('kgj1','nb1','u1','incremental','running','extracting',1,0,0,"
            "'','',?,?,'')",
            (NOW, NOW),
        )
    runner = repo._runtime.notebook_delete
    runner._quiesce_timeout_seconds = 0.2
    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    runner.run(job["id"])
    row = repo._runtime.notebook_delete_jobs.get(job["id"])
    assert row["phase"] == "paths"
    assert row["status"] == "waiting"


def test_quiesce_never_sleeps_past_half_the_sweep_stale_cutoff(repo, monkeypatch):
    """P2-a (codex PR#659 round 1): the quiesce backoff (caps at 60s) must
    never sleep longer than half of ``NOTEBOOK_DELETE_SWEEP_SECONDS`` (the
    SAME cutoff ``mark_running``/``list_stale`` use), or a sweep configured
    below 60s can judge a still-alive, still-legitimately-polling worker
    'stale' and steal its lease via driver A's resubmit — the two workers
    then requeue each other indefinitely.

    Drives the real backoff sequence (5, 10, 20, 40, 60, 60, ...) through
    many rounds with a fully controlled fake clock (no real sleeping), then
    asserts every single ``time.sleep(...)`` call the loop actually issued
    stayed within the cap. Mutation: drop the cap (``time.sleep(min(backoff,
    remaining))``) and this goes red immediately, since backoff alone grows
    to 60s while the cap here is 5s."""
    from app.services import notebook_delete as nd

    _seed_user_and_notebook(repo)
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO kg_build_jobs (id,notebook_id,created_by,mode,status,"
            "stage,total_sources,completed_sources,failed_sources,error_code,"
            "error_message,created_at,updated_at,finished_at) VALUES "
            "('kgj1','nb1','u1','incremental','running','extracting',1,0,0,"
            "'','',?,?,'')",
            (NOW, NOW),
        )
    runner = repo._runtime.notebook_delete
    runner._sweep_seconds = 10  # cap = max(1, 10/2) = 5
    runner._quiesce_timeout_seconds = 200  # many backoff rounds, fake clock only
    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")

    fake_clock = [0.0]
    sleep_calls: list[float] = []

    def fake_monotonic():
        return fake_clock[0]

    def fake_sleep(duration):
        sleep_calls.append(duration)
        fake_clock[0] += duration

    monkeypatch.setattr(nd.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(nd.time, "sleep", fake_sleep)

    runner.run(job["id"])

    row = repo._runtime.notebook_delete_jobs.get(job["id"])
    assert row["status"] == "waiting"  # quiesce eventually timed out (fake clock)
    lease_token = row["lease_token"]
    assert len(sleep_calls) >= 5, "退避轮数不足以覆盖到会撞 60s 上限的那几轮"
    cap = max(1.0, runner._sweep_seconds / 2.0)
    assert all(duration <= cap for duration in sleep_calls), (
        f"某段睡眠 {max(sleep_calls)}s 超过了 stale cutoff 的一半 {cap}s —— "
        "扫尾可能把这个仍在正常退避的 worker 判 stale 抢租"
    )
    # 每次心跳(advance_phase)都带着同一个 lease_token 写——全程未被抢走。
    assert lease_token


# ---------------------------------------------------------------------------
# 3. The three in-flight-rebuild checkpoints (§4.2 option A)
# ---------------------------------------------------------------------------


def test_relink_checkpoint_aborts_when_notebook_is_deleting(repo):
    """变异钉:删掉 relink 循环里的检查点会让本用例变红——relink_notebook_kg
    会正常跑完并返回统计,而不是抛出 NotebookDeletingAbortsMaintenanceError。

    notebook 本身仍留在 'ready'(不是 'deleting'):`get_notebook()` 在方法
    入口就用 NOTEBOOK_LIVE_SQL 过滤,若真把库置成 deleting 会在到达循环
    检查点之前就先 404——那是另一层(T-1 谓词),不是本用例要测的循环内
    检查点。直接注入 `_notebook_deleting` 隔离测「循环里那一次点查」本身。"""
    from app.repositories.ports import NotebookDeletingAbortsMaintenanceError

    _seed_user_and_notebook(repo)
    _seed_source(repo, "nb1", "s1")
    lifecycle = repo._runtime.knowledge_lifecycle
    lifecycle._notebook_deleting = lambda _nid: True
    with pytest.raises(NotebookDeletingAbortsMaintenanceError):
        lifecycle.relink_notebook_kg("nb1")


def test_relink_checkpoint_does_not_fire_when_not_deleting(repo):
    """反向对照:notebook 仍 active 时 relink 正常跑完,不抛出中止异常。"""
    _seed_user_and_notebook(repo)
    _seed_source(repo, "nb1", "s1")
    lifecycle = repo._runtime.knowledge_lifecycle
    stats = lifecycle.relink_notebook_kg("nb1")
    assert stats == {"isolated_before": 0, "edges_added": 0, "isolated_after": 0}


def test_unified_rebuild_checkpoint_aborts_when_notebook_is_deleting(repo):
    from app.repositories.ports import NotebookDeletingAbortsMaintenanceError

    _seed_user_and_notebook(repo)
    lifecycle = repo._runtime.knowledge_lifecycle
    # Force _notebook_deleting True without needing a real 'deleting' row
    # (get_notebook() would 404 first otherwise) — isolates the _stage
    # checkpoint itself.
    lifecycle._notebook_deleting = lambda _nid: True
    with pytest.raises(NotebookDeletingAbortsMaintenanceError):
        lifecycle.rebuild_unified_kg("nb1", force=True)


def test_notebook_deleting_callable_reads_raw_status_not_the_live_filter(repo):
    """校验 `_notebook_deleting` 真的读裸 status(不经 NOTEBOOK_LIVE_SQL),
    且只在恰好 'deleting' 时为真——'copying' 不触发(那不是本设计的信号)。"""
    _seed_user_and_notebook(repo, status="copying")
    rt = repo._runtime
    assert rt._notebook_kg_maintenance_running("nb1") is False  # unrelated axis
    assert rt.notebook_store.status_of("nb1") == "copying"
    with rt.database.write() as db:
        db.execute("UPDATE notebooks SET status='deleting' WHERE id='nb1'")
    assert rt.notebook_store.status_of("nb1") == "deleting"


# ---------------------------------------------------------------------------
# 4. Phase 5 finalize: atomicity + archive equivalence (G1 hard gate)
# ---------------------------------------------------------------------------


def _seed_full_activity(repo, notebook_id, owner, suffix):
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO ask_jobs (id,notebook_id,created_by,mode,status,"
            "question,created_at,updated_at,asked_at) VALUES "
            "(?,?,?,?,?,?,?,?,?)",
            (f"ask-{suffix}", notebook_id, owner, "chunk", "done",
             "why does gain stabilize", NOW, NOW, NOW),
        )
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,file_path,source_type,"
            "status,parse_status,uploaded_by,created_at,updated_at) VALUES "
            "(?,?,?,?,?,?,?,?,?,?)",
            (f"src-{suffix}", notebook_id, "Fixture", f"/tmp/{suffix}.pdf",
             "pdf", "ready", "parsed", owner, NOW, NOW),
        )
        db.execute(
            "INSERT INTO reports (id,notebook_id,created_by,question,status,"
            "depth,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (f"rep-{suffix}", notebook_id, owner, "why does gain stabilize",
             "done", 1, NOW, NOW),
        )


_ARCHIVE_COLUMNS_EXCLUDING_LINKS = (
    # notebook_id/record_id/notebook_name deliberately excluded — the two
    # notebooks in this test have different ids by construction (each path
    # needs its OWN notebook row) so their derived "NB-{id}" names legitimately
    # differ; everything else must match byte-for-byte.
    "activity_type,notebook_owner_id,created_at,updated_at,"
    "asked_at,question,mode,status,display_title,file_name,source_type,"
    "parse_status,parse_failed,depth"
)


def test_phase5_archive_output_is_byte_identical_to_the_legacy_synchronous_path(repo):
    """G1 硬门:相位 5(job_id 给定)与今天的单事务路径(job_id=None)必须逐字段
    相等——这里用两本内容对齐、id 前缀不同的库分别走两条路径,比较除
    notebook_id/record_id 外的全部字段。"""
    _seed_user_and_notebook(repo, notebook_id="nb-legacy", owner="u1")
    _seed_full_activity(repo, "nb-legacy", "u1", "legacy")
    _seed_user_and_notebook(repo, notebook_id="nb-jobized", owner="u1")
    _seed_full_activity(repo, "nb-jobized", "u1", "jobized")

    store = repo._runtime.notebook_store
    legacy_paths = store.delete_row_and_orphan_embeddings("nb-legacy")
    jobized_job = repo._runtime.notebook_delete_jobs.request("nb-jobized", "u1")
    # request() already CAS'd notebooks.status to 'deleting' — finalize does
    # not require that (its FOR UPDATE lock only cares the row still exists).
    jobized_paths = store.delete_row_and_orphan_embeddings(
        "nb-jobized", job_id=jobized_job["id"]
    )
    assert legacy_paths == ["/tmp/legacy.pdf"]
    assert jobized_paths == ["/tmp/jobized.pdf"]

    with repo._runtime.database.connect() as conn:
        legacy_rows = conn.execute(
            f"SELECT {_ARCHIVE_COLUMNS_EXCLUDING_LINKS} FROM retained_user_activity "
            "WHERE notebook_id='nb-legacy' ORDER BY activity_type"
        ).fetchall()
        jobized_rows = conn.execute(
            f"SELECT {_ARCHIVE_COLUMNS_EXCLUDING_LINKS} FROM retained_user_activity "
            "WHERE notebook_id='nb-jobized' ORDER BY activity_type"
        ).fetchall()
    assert len(legacy_rows) == len(jobized_rows) == 3
    for legacy_row, jobized_row in zip(legacy_rows, jobized_rows):
        assert dict(legacy_row) == dict(jobized_row)


def test_phase5_deletes_job_and_side_table_rows_in_the_same_transaction(repo):
    _seed_user_and_notebook(repo)
    _seed_source(repo, "nb1", "s1", "/tmp/s1.pdf")
    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    repo._runtime.notebook_delete_jobs.materialize_paths_page(job["id"], "nb1", "", 500)

    repo._runtime.notebook_store.delete_row_and_orphan_embeddings(
        "nb1", job_id=job["id"]
    )
    with repo._runtime.database.connect() as conn:
        jobs = conn.execute("SELECT COUNT(*) AS c FROM notebook_delete_jobs").fetchone()
        files = conn.execute("SELECT COUNT(*) AS c FROM notebook_delete_files").fetchone()
    assert jobs["c"] == 0
    assert files["c"] == 0


def test_phase5_early_return_branch_still_cleans_up_job_bookkeeping(repo):
    """§T-4 的「作业行在、notebooks 行不在」特例:notebooks 行已经被带外删除,
    finalize 的早退分支也要清掉 job/side-table 行,不留残渣。"""
    _seed_user_and_notebook(repo)
    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    with repo._runtime.database.write() as db:
        db.execute("DELETE FROM notebooks WHERE id='nb1'")  # out-of-band delete

    file_paths = repo._runtime.notebook_store.delete_row_and_orphan_embeddings(
        "nb1", job_id=job["id"]
    )
    assert file_paths == []
    with repo._runtime.database.connect() as conn:
        jobs = conn.execute("SELECT COUNT(*) AS c FROM notebook_delete_jobs").fetchone()
    assert jobs["c"] == 0


def test_legacy_caller_without_job_id_is_unaffected(repo):
    """`job_id=None`(全部既有调用点)必须逐字节沿用旧行为——不新增 DELETE、
    不触发任何 D-4 超时旋钮。"""
    _seed_user_and_notebook(repo)
    _seed_source(repo, "nb1", "s1", "/tmp/s1.pdf")
    paths = repo._runtime.notebook_store.delete_row_and_orphan_embeddings("nb1")
    assert paths == ["/tmp/s1.pdf"]
    with repo._runtime.database.connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM notebooks WHERE id='nb1'").fetchone()
    assert row["c"] == 0


# ---------------------------------------------------------------------------
# 5. Sweep's two drivers (§T-4)
# ---------------------------------------------------------------------------


def test_sweep_driver_a_resumes_a_stale_active_job(repo):
    _seed_user_and_notebook(repo)
    _seed_source(repo, "nb1", "s1", "/tmp/s1.pdf")
    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    repo._runtime.notebook_delete_jobs.mark_running(job["id"], stale_cutoff_seconds=300)
    # Simulate a dead worker: updated_at far in the past.
    with repo._runtime.database.write() as db:
        db.execute(
            "UPDATE notebook_delete_jobs SET updated_at='2000-01-01T00:00:00' "
            "WHERE id=?",
            (job["id"],),
        )

    runner = repo._runtime.notebook_delete
    runner._sweep_seconds = 1
    submitted = runner.sweep_once()
    assert submitted == 1
    _drain()
    with repo._runtime.database.connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM notebooks WHERE id='nb1'").fetchone()
    assert row["c"] == 0


def test_sweep_driver_a_does_not_touch_fresh_active_jobs(repo):
    _seed_user_and_notebook(repo)
    job = repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    runner = repo._runtime.notebook_delete
    runner._sweep_seconds = 300
    assert runner.sweep_once() == 0
    still_there = repo._runtime.notebook_delete_jobs.get(job["id"])
    assert still_there["status"] == "queued"


def test_sweep_driver_b_recreates_a_missing_job_for_a_deleting_notebook(repo):
    """CAS 提交了但作业行的 INSERT 失败(或作业行被带外删除)——扫尾必须补建。"""
    _seed_user_and_notebook(repo, status="deleting")
    runner = repo._runtime.notebook_delete
    submitted = runner.sweep_once()
    assert submitted == 1
    _drain()
    with repo._runtime.database.connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM notebooks WHERE id='nb1'").fetchone()
    assert row["c"] == 0


def test_sweep_driver_b_is_a_noop_when_every_deleting_notebook_has_a_job(repo):
    _seed_user_and_notebook(repo)
    repo._runtime.notebook_delete_jobs.request("nb1", "u1")
    runner = repo._runtime.notebook_delete
    runner._sweep_seconds = 300
    assert runner.sweep_once() == 0
    _drain()


def test_recreate_for_deleting_notebook_returns_existing_job_on_race(repo):
    """两次并发的 recreate 只能有一行赢,另一次必须拿到赢家的作业行,而不是
    让部分唯一索引的 IntegrityError 冒泡出去。"""
    _seed_user_and_notebook(repo, status="deleting")
    store = repo._runtime.notebook_delete_jobs
    first = store.recreate_for_deleting_notebook("nb1")
    second = store.recreate_for_deleting_notebook("nb1")
    assert first["id"] == second["id"]


# ---------------------------------------------------------------------------
# 6. Background job pool routing
# ---------------------------------------------------------------------------


def test_delete_job_name_routes_to_the_delete_pool_not_heavy_or_light():
    resolved = background_jobs._maintenance_pool("deletenb-nb1")
    assert resolved == (background_jobs._DELETE_POOL, "deletenb")


# ---------------------------------------------------------------------------
# 7. Startup wiring (§T-4's "启动后 + 每 N 秒" sweep cadence)
# ---------------------------------------------------------------------------


def test_run_startup_wires_and_stops_the_notebook_delete_sweeper(monkeypatch):
    """变异钉:如果 `_start_notebook_delete_sweeper`/`_stop_notebook_delete_sweeper`
    没有真的接进 `run_startup`/`close_repository`(例如只是定义了函数、忘了调用),
    `notebook_delete.py` 的双驱动扫尾永远不会在生产里自动跑——本用例直接钉住
    「启动一次真实的 lifecycle 后,扫尾线程确实起来了;lease 关闭后确实停了」。"""
    monkeypatch.setenv("NOTEBOOK_DELETE_SWEEP_SECONDS", "1")
    from app.core import readiness
    from app.services import notebook_delete as nd_module
    from app.services import startup_warmup

    lease = startup_warmup.begin_lifecycle()
    assert lease is not None
    repo = startup_warmup.run_startup(lease)
    try:
        assert readiness.is_ready() is True
        state = startup_warmup._active_lifecycle
        assert state is not None
        assert state.notebook_delete_sweeper_stop is not None
        assert nd_module._active is not None
        time.sleep(1.5)  # one tick fires without raising (interval is 1s)
        assert nd_module._active is not None  # thread still alive
    finally:
        startup_warmup.close_repository(lease, repo)
    assert nd_module._active is None
