"""「删除知识图谱」:知识图谱维护槽里的第三种后台任务。

钉的是可观察行为:
  · 端点形状(202 风格返回 + 状态轮询)与删了什么 / 留了什么;
  · 与「补上关联」「重新合并」共用一个槽,409 点名真正占槽的动作;
  · 与分析作业双向互斥,且删除期间点分析得到的是「正在删除知识图谱」而不是
    「分析任务正在运行」(删除任务从不占 kg_building 标记);
  · 每条退出路径都释放槽(提交失败、删除抛错、BaseException、删库打断);
  · 删除后笔记本摘要不再复述上一次分析的结果,但作业行本身保留。
"""
from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.ports import (
    KgBuildAlreadyRunning,
    KgMaintenanceAlreadyRunning,
)
from app.services.sqlite_repository import SQLiteRepository, _now
from tests.model_testkit import bind_chat_client


DELETE_BUSY = "当前笔记本正在删除知识图谱，请等它完成"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


@pytest.fixture
def api(tmp_path, monkeypatch):
    """TestClient bound to one real repository; background submission is
    captured instead of run, so each test decides when (and whether) the
    worker body executes."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.api import deps
    from app.main import app
    from app.services import background_jobs

    client = TestClient(app)
    real_repo = deps.repository()
    monkeypatch.setattr(deps, "repository", lambda: real_repo)
    submitted: list[tuple] = []
    monkeypatch.setattr(
        background_jobs,
        "submit",
        lambda fn, *args, **kwargs: submitted.append((fn, args, kwargs)),
    )
    return client, real_repo, submitted


def _seed_graph(repo, notebook_id: str, *, with_memory: bool = True) -> None:
    """One user document with TWO KG objects and one relation, and (unless
    ``with_memory`` is False) one hidden Memory projection source with its own
    object + relation. The document's object and relation counts differ on
    purpose, so a swapped counter mapping cannot pass."""
    now = _now()
    sources = [("src-doc", "markdown")]
    if with_memory:
        sources.append(("src-mem", "memory"))
    with repo._write() as db:
        for source_id, source_type in sources:
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,status,"
                "parse_status,file_name,file_path,file_size,file_hash,summary,"
                "doc_type,created_at,updated_at) "
                "VALUES (?,?,?,?,'extracted','parsed','','',0,?,'','',?,?)",
                (source_id, notebook_id, source_id, source_type, source_id,
                 now, now),
            )
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,"
                "location_label,text,metadata,created_at) "
                "VALUES (?,?,'paragraph','p1','hello','{}',?)",
                (f"el-{source_id}", source_id, now),
            )
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,"
                "status,owner,payload,evidence,source_candidate_id,source_id,"
                "created_at,updated_at) "
                "VALUES (?,?,'concept','approved','','{}','[]',NULL,?,?,?)",
                (f"ko-{source_id}", notebook_id, source_id, now, now),
            )
            db.execute(
                "INSERT INTO knowledge_relations (id,notebook_id,source_id,"
                "source_object_id,target_object_id,edge_type,evidence,"
                "created_at) VALUES (?,?,?,?,?,'relates_to','[]',?)",
                (f"rel-{source_id}", notebook_id, source_id,
                 f"ko-{source_id}", f"ko-{source_id}", now),
            )
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,"
            "status,owner,payload,evidence,source_candidate_id,source_id,"
            "created_at,updated_at) "
            "VALUES (?,?,'concept','approved','','{}','[]',NULL,?,?,?)",
            ("ko-src-doc-2", notebook_id, "src-doc", now, now),
        )


def _count(repo, sql: str, params: tuple) -> int:
    with repo._connect() as db:
        return int(db.execute(sql, params).fetchone()[0])


def _finished_build_job(repo, notebook_id: str) -> str:
    jobs = repo._runtime.kg_build_jobs
    job = jobs.create_job(notebook_id, "", "incremental", 1)
    assert jobs.finish(job["id"], "succeeded")
    return job["id"]


# ---------------------------------------------------------------------------
# End-to-end through the HTTP surface
# ---------------------------------------------------------------------------


def test_delete_removes_document_graph_keeps_sources_and_clears_build_summary(api):
    client, repo, submitted = api
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    _seed_graph(repo, nb)
    old_job_id = _finished_build_job(repo, nb)
    assert repo.get_notebook(nb).kg_build.job_id == old_job_id
    # No model configured: deleting must not need one.
    bind_chat_client(repo, "kg_extract", MagicMock(configured=False))

    idle = client.get(f"/api/notebooks/{nb}/kg/delete/status").json()
    assert idle == {
        "job_id": "", "notebook_id": nb, "status": "idle", "running": False,
        "objects_deleted": 0, "relations_deleted": 0,
    }

    started = client.post(f"/api/notebooks/{nb}/kg/delete")
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["status"] == "deleting"
    assert body["notebook_id"] == nb
    assert body["job_id"].startswith("kdj-")
    # The request thread only claimed and submitted; nothing is deleted yet.
    assert len(submitted) == 1
    fn, args, kwargs = submitted[0]
    assert args == (nb, body["job_id"])
    assert kwargs["name"] == f"deletekg-{nb}"
    running = client.get(f"/api/notebooks/{nb}/kg/delete/status").json()
    assert running["status"] == "running" and running["running"] is True
    assert running["job_id"] == body["job_id"]
    assert _count(repo, "SELECT COUNT(*) FROM knowledge_objects WHERE id=?",
                  ("ko-src-doc",)) == 1

    fn(*args)

    done = client.get(f"/api/notebooks/{nb}/kg/delete/status").json()
    assert done == {
        "job_id": body["job_id"], "notebook_id": nb, "status": "succeeded",
        "running": False, "objects_deleted": 2, "relations_deleted": 1,
    }
    # Document-derived graph gone; Memory projection graph, sources and
    # parsed elements kept.
    assert _count(repo, "SELECT COUNT(*) FROM knowledge_objects WHERE id "
                  "IN ('ko-src-doc','ko-src-doc-2')", ()) == 0
    assert _count(repo, "SELECT COUNT(*) FROM knowledge_relations WHERE id=?",
                  ("rel-src-doc",)) == 0
    assert _count(repo, "SELECT COUNT(*) FROM knowledge_objects WHERE id=?",
                  ("ko-src-mem",)) == 1
    assert _count(repo, "SELECT COUNT(*) FROM knowledge_relations WHERE id=?",
                  ("rel-src-mem",)) == 1
    assert _count(repo, "SELECT COUNT(*) FROM sources WHERE notebook_id=?",
                  (nb,)) == 2
    assert _count(repo, "SELECT COUNT(*) FROM source_elements WHERE source_id "
                  "IN ('src-doc','src-mem')", ()) == 2

    # The previous build result no longer describes the graph, so the summary
    # stops narrating it — but the job row stays (admin usage counts rows).
    summary = client.get(f"/api/notebooks/{nb}").json()
    assert summary["kg_build"] is None
    assert repo.get_notebook(nb).kg_build is None
    assert _count(repo, "SELECT COUNT(*) FROM kg_build_jobs WHERE notebook_id=?",
                  (nb,)) == 1

    # A later analysis is visible again.
    new_job = repo._runtime.kg_build_jobs.create_job(nb, "", "incremental", 0)
    assert repo.get_notebook(nb).kg_build.job_id == new_job["id"]


def test_delete_endpoints_404_for_unknown_notebook(api):
    client, _repo, submitted = api
    assert client.post("/api/notebooks/nb-missing/kg/delete").status_code == 404
    assert client.get(
        "/api/notebooks/nb-missing/kg/delete/status"
    ).status_code == 404
    assert submitted == []


def _login(client: TestClient, username: str) -> dict:
    client.post(
        "/api/auth/register", json={"username": username, "password": "pw123456"}
    )
    token = client.post(
        "/api/auth/login", json={"username": username, "password": "pw123456"}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_read_only_member_cannot_start_delete_but_can_poll_status(api):
    client, repo, submitted = api
    owner = _login(client, "d00000001")
    nb = client.post(
        "/api/notebooks", json={"name": "L"}, headers=owner
    ).json()["id"]
    member = _login(client, "d00000002")
    member_id = client.get("/api/me", headers=member).json()["id"]
    repo.add_member(nb, member_id)

    refused = client.post(f"/api/notebooks/{nb}/kg/delete", headers=member)
    # The kg:write capability guard's established refusal (no existence leak).
    assert refused.status_code == 404
    assert submitted == []
    status = client.get(f"/api/notebooks/{nb}/kg/delete/status", headers=member)
    assert status.status_code == 200
    assert status.json()["status"] == "idle"

    allowed = client.post(f"/api/notebooks/{nb}/kg/delete", headers=owner)
    assert allowed.status_code == 200, allowed.text


def test_submission_failure_settles_failed_and_frees_the_slot(api, monkeypatch):
    client, repo, _submitted = api
    from app.services import background_jobs

    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

    def _boom(*_args, **_kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(background_jobs, "submit", _boom)
    no_raise = TestClient(client.app, raise_server_exceptions=False)
    assert no_raise.post(f"/api/notebooks/{nb}/kg/delete").status_code == 500

    status = repo.kg_delete_status(nb)
    assert status["status"] == "failed" and status["running"] is False
    repo.start_kg_delete(nb)


# ---------------------------------------------------------------------------
# Shared slot: exclusivity with relink / rebuild and 409 copy naming the holder
# ---------------------------------------------------------------------------


def test_running_delete_refuses_relink_rebuild_and_a_second_delete(api):
    client, repo, submitted = api
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    assert client.post(f"/api/notebooks/{nb}/kg/delete").status_code == 200

    for path in ("kg/relink", "unified-kg/rebuild", "kg/delete"):
        refused = client.post(f"/api/notebooks/{nb}/{path}")
        assert refused.status_code == 409, path
        assert refused.json()["detail"] == DELETE_BUSY, path
    assert len(submitted) == 1
    # The other kinds' polls are not parked on the delete job.
    assert repo.notebook_relink_status(nb)["status"] == "idle"
    assert repo.unified_kg_rebuild_status(nb)["status"] == "idle"


@pytest.mark.parametrize(
    ("start", "copy"),
    (
        ("start_notebook_relink", "当前笔记本正在补上关联，请等它完成"),
        ("start_unified_kg_rebuild", "当前笔记本正在重新合并，请等它完成"),
    ),
)
def test_running_relink_or_rebuild_refuses_delete_naming_the_holder(
    api, start, copy
):
    client, repo, submitted = api
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    getattr(repo, start)(nb)

    refused = client.post(f"/api/notebooks/{nb}/kg/delete")
    assert refused.status_code == 409
    assert refused.json()["detail"] == copy
    assert submitted == []
    assert repo.kg_delete_status(nb)["status"] == "idle"


def test_running_analysis_refuses_delete_with_the_build_copy(api):
    client, repo, submitted = api
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    lifecycle = repo._runtime.knowledge_lifecycle
    lifecycle.prepare_notebook_kg_job(nb, "incremental", allow_without_model=True)

    refused = client.post(f"/api/notebooks/{nb}/kg/delete")
    assert refused.status_code == 409
    assert refused.json()["detail"] == "当前笔记本已有知识图谱分析任务正在运行"
    assert submitted == []
    assert lifecycle.kg_maintenance.active_kind(nb) is None


def test_analysis_started_during_delete_is_refused_with_holder_delete(api):
    client, repo, _submitted = api
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    bind_chat_client(repo, "kg_extract", MagicMock(configured=True))
    lifecycle = repo._runtime.knowledge_lifecycle
    assert client.post(f"/api/notebooks/{nb}/kg/delete").status_code == 200

    for path in ("kg/build", "kg/rebuild"):
        refused = client.post(f"/api/notebooks/{nb}/{path}")
        assert refused.status_code == 409, path
        assert refused.json()["detail"] == DELETE_BUSY, path
    with pytest.raises(KgMaintenanceAlreadyRunning) as exc:
        lifecycle.prepare_notebook_kg_job(
            nb, "incremental", allow_without_model=True)
    assert exc.value.holder == "delete"
    assert not lifecycle._kg_build_active(nb)
    assert _count(repo, "SELECT COUNT(*) FROM kg_build_jobs WHERE notebook_id=?",
                  (nb,)) == 0


def test_durable_analysis_row_from_another_process_refuses_delete(api):
    """A running ``kg_build_jobs`` row with no in-process marker — e.g. a
    ``batch_ingest`` CLI analysis run alongside the backend — must refuse the
    delete with the build 409 instead of draining what it is extracting. The
    refused claim frees the slot and leaves the previous terminal delete
    readable."""
    client, repo, submitted = api
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    lifecycle = repo._runtime.knowledge_lifecycle
    assert client.post(f"/api/notebooks/{nb}/kg/delete").status_code == 200
    fn, args, _kwargs = submitted[0]
    fn(*args)
    finished = client.get(f"/api/notebooks/{nb}/kg/delete/status").json()
    assert finished["status"] == "succeeded"

    other_process_row = repo._runtime.kg_build_jobs.create_job(
        nb, "", "incremental", 0
    )
    assert not lifecycle._kg_build_active(nb)

    refused = client.post(f"/api/notebooks/{nb}/kg/delete")
    assert refused.status_code == 409
    assert refused.json()["detail"] == "当前笔记本已有知识图谱分析任务正在运行"
    assert len(submitted) == 1
    assert lifecycle.kg_maintenance.active_kind(nb) is None
    assert client.get(f"/api/notebooks/{nb}/kg/delete/status").json() == finished

    assert repo._runtime.kg_build_jobs.finish(other_process_row["id"], "succeeded")
    assert client.post(f"/api/notebooks/{nb}/kg/delete").status_code == 200


def test_failing_durable_probe_revokes_the_claim(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    lifecycle = repo._runtime.knowledge_lifecycle

    def _unavailable(_notebook_id):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(repo._runtime.kg_build_jobs, "has_running", _unavailable)
    with pytest.raises(RuntimeError):
        repo.start_kg_delete(nb)
    assert lifecycle.kg_maintenance.active_kind(nb) is None
    assert repo.kg_delete_status(nb)["status"] == "idle"


def test_maintenance_status_polls_check_the_live_row_not_the_full_summary(
    repo, monkeypatch
):
    """Status views are polled (the delete poll every few seconds, and opening
    a notebook reads three of them); each must not assemble a whole
    NotebookSummary, yet a missing or tombstoned notebook still raises
    KeyError (the routes' 404). The slot claim is held to the same probe —
    the route already built one full summary before it."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id

    def _summary_forbidden(_notebook_id):
        raise AssertionError("a status poll built a NotebookSummary")

    monkeypatch.setattr(repo._runtime.catalog, "get_notebook", _summary_forbidden)
    for status in (
        repo.kg_delete_status,
        repo.notebook_relink_status,
        repo.unified_kg_rebuild_status,
    ):
        assert status(nb)["status"] == "idle"
    claimed = repo.start_kg_delete(nb)
    repo.fail_kg_delete_submission(nb, claimed["job_id"])
    assert repo.kg_delete_status(nb)["status"] == "failed"
    with pytest.raises(KeyError):
        repo.start_kg_delete("nb-missing")
    with pytest.raises(KeyError):
        repo.kg_delete_status("nb-missing")
    with repo._write() as db:
        db.execute("UPDATE notebooks SET status='deleting' WHERE id=?", (nb,))
    with pytest.raises(KeyError):
        repo.kg_delete_status(nb)


# ---------------------------------------------------------------------------
# Worker body: fence, ordering and every exit path
# ---------------------------------------------------------------------------


def test_delete_in_flight_never_marks_analysis_running_and_holds_quiesce_leg_b(
    repo, monkeypatch
):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    _seed_graph(repo, nb)
    _finished_build_job(repo, nb)
    lifecycle = repo._runtime.knowledge_lifecycle
    original = lifecycle._delete_notebook_kg_fenced
    entered = threading.Event()
    release = threading.Event()
    observed: dict = {}

    def _blocking(notebook_id):
        # Job history is already cleared when the first drain would start.
        observed["kg_build_at_drain"] = repo.get_notebook(notebook_id).kg_build
        entered.set()
        assert release.wait(timeout=10)
        return original(notebook_id)

    monkeypatch.setattr(lifecycle, "_delete_notebook_kg_fenced", _blocking)
    job = repo.start_kg_delete(nb)
    worker = threading.Thread(
        target=repo.run_kg_delete_job, args=(nb, job["job_id"])
    )
    worker.start()
    try:
        assert entered.wait(timeout=10)
        assert observed["kg_build_at_drain"] is None
        summary = repo.get_notebook(nb)
        assert summary.kg_building is False
        assert not lifecycle._kg_build_active(nb)
        assert repo._runtime._notebook_kg_maintenance_running(nb) is True
        assert repo.kg_delete_status(nb)["running"] is True
        with pytest.raises(KgMaintenanceAlreadyRunning) as exc:
            lifecycle.prepare_notebook_kg_job(
                nb, "rebuild", allow_without_model=True)
        assert exc.value.holder == "delete"
        assert not isinstance(exc.value, KgBuildAlreadyRunning)
    finally:
        release.set()
        worker.join(timeout=10)

    assert not worker.is_alive()
    assert repo._runtime._notebook_kg_maintenance_running(nb) is False
    assert repo.kg_delete_status(nb)["status"] == "succeeded"


def test_failing_delete_settles_failed_with_a_counts_only_event(
    repo, monkeypatch
):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    lifecycle = repo._runtime.knowledge_lifecycle
    events: list[dict] = []
    monkeypatch.setattr(repo._runtime.event_log, "emit", events.append)

    def _explode(_notebook_id):
        raise RuntimeError("secret /private/path")

    monkeypatch.setattr(lifecycle, "_delete_notebook_kg_fenced", _explode)
    job = repo.start_kg_delete(nb)
    with pytest.raises(RuntimeError):
        repo.run_kg_delete_job(nb, job["job_id"])

    status = repo.kg_delete_status(nb)
    assert status["status"] == "failed" and status["running"] is False
    assert status["objects_deleted"] == 0 and status["relations_deleted"] == 0
    assert events == [{"kind": "kg_delete_failed", "notebook_id": nb}]
    repo.start_kg_delete(nb)


def test_successful_delete_emits_counts_only_event(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    _seed_graph(repo, nb)
    events: list[dict] = []
    monkeypatch.setattr(repo._runtime.event_log, "emit", events.append)
    job = repo.start_kg_delete(nb)

    assert repo.run_kg_delete_job(nb, job["job_id"]) == {
        "objects_deleted": 2, "relations_deleted": 1,
    }
    assert [e for e in events if e.get("kind") == "kg_deleted"] == [{
        "kind": "kg_deleted", "notebook_id": nb,
        "objects_deleted": 2, "relations_deleted": 1,
    }]


def test_interrupted_delete_still_releases_the_slot(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    lifecycle = repo._runtime.knowledge_lifecycle

    def _interrupted(_notebook_id):
        raise KeyboardInterrupt

    monkeypatch.setattr(lifecycle, "_delete_notebook_kg_fenced", _interrupted)
    job = repo.start_kg_delete(nb)
    with pytest.raises(KeyboardInterrupt):
        repo.run_kg_delete_job(nb, job["job_id"])

    assert repo.kg_delete_status(nb)["status"] == "failed"
    assert lifecycle.kg_maintenance.active_kind(nb) is None


def test_notebook_delete_landing_mid_job_fails_the_job_and_frees_the_slot(
    repo, monkeypatch
):
    from app.repositories.ports import NotebookDeletingAbortsMaintenanceError

    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    _seed_graph(repo, nb)
    lifecycle = repo._runtime.knowledge_lifecycle
    monkeypatch.setattr(lifecycle, "_notebook_deleting", lambda _nb: True)
    job = repo.start_kg_delete(nb)

    with pytest.raises(NotebookDeletingAbortsMaintenanceError):
        repo.run_kg_delete_job(nb, job["job_id"])

    assert repo.kg_delete_status(nb)["status"] == "failed"
    assert repo._runtime._notebook_kg_maintenance_running(nb) is False


# ---------------------------------------------------------------------------
# Graph-view preview artifact: the first read after a delete must be correct
# ---------------------------------------------------------------------------


def _published_preview(repo, notebook_id: str, monkeypatch):
    """Publish the standalone graph-view preview and warm it with the exact
    read the frontend sends. Background refresh spawns are recorded, never
    run, so a test sees what the read itself served."""
    scale = repo._runtime.scale_artifacts
    spawned: list[str] = []
    monkeypatch.setattr(
        scale, "_start_daemon", lambda name, _target: spawned.append(name)
    )
    assert scale.build_viz(notebook_id) is not None
    warm = repo.unified_graph(notebook_id, level="object", limit=80)
    live = Path(str(repo._runtime.scale_artifact_store.viz_dir(notebook_id)))
    assert (live / "manifest.json").exists()
    return scale, warm, live, spawned


def _run_delete(repo, notebook_id: str) -> dict:
    job = repo.start_kg_delete(notebook_id)
    return repo.run_kg_delete_job(notebook_id, job["job_id"])


def test_first_object_read_after_delete_does_not_repaint_deleted_nodes(
    repo, monkeypatch
):
    """Mutation anchor: drop the post-delete preview refresh and this first
    read serves the stale preview (the two deleted nodes) while it spawns the
    refresh that would only fix the NEXT read."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    _seed_graph(repo, nb, with_memory=False)
    scale, warm, live, spawned = _published_preview(repo, nb, monkeypatch)
    assert warm["total_nodes"] == 2
    version_before = scale.version(nb)

    assert _run_delete(repo, nb) == {"objects_deleted": 2, "relations_deleted": 1}

    first = repo.unified_graph(nb, level="object", limit=80)
    assert first["nodes"] == [] and first["total_nodes"] == 0
    assert not live.exists()
    assert spawned == []
    # The scale-EMBEDDED preview needs no refresh: it is served only through
    # load()'s exact version match, and the delete moved the version.
    assert scale.version(nb) != version_before


def test_delete_republishes_the_preview_for_hidden_objects_that_remain(
    repo, monkeypatch
):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    _seed_graph(repo, nb)
    scale, warm, live, spawned = _published_preview(repo, nb, monkeypatch)
    assert warm["total_nodes"] == 3

    _run_delete(repo, nb)

    first = repo.unified_graph(nb, level="object", limit=80)
    assert [node["id"] for node in first["nodes"]] == ["ko-src-mem"]
    assert (live / "manifest.json").exists()
    assert spawned == []


def test_over_budget_delete_retires_the_preview_without_building(
    repo, monkeypatch
):
    """Over the in-process viz budget the refresh must not materialise the
    remaining graph here; it retires the stale preview so the view reports
    "no preview yet" rather than the deleted nodes."""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    _seed_graph(repo, nb)
    scale, _warm, live, _spawned = _published_preview(repo, nb, monkeypatch)
    monkeypatch.setattr(scale.settings, "viz_sync_build_max_objects", 0)

    def _no_build(_notebook_id):
        raise AssertionError("an over-budget viz build ran in-process")

    monkeypatch.setattr(scale.builder, "build_viz", _no_build)

    _run_delete(repo, nb)

    assert not live.exists()
    first = repo.unified_graph(nb, level="object", limit=80)
    assert first["nodes"] == []


def test_preview_refresh_held_elsewhere_or_failing_does_not_fail_the_delete(
    repo, monkeypatch
):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    _seed_graph(repo, nb, with_memory=False)
    scale, _warm, live, _spawned = _published_preview(repo, nb, monkeypatch)
    # "Provably held by somebody else" (a CLI import, another replica).
    monkeypatch.setattr(scale, "_scale_build_lock", lambda _nb: None)

    assert _run_delete(repo, nb) == {"objects_deleted": 2, "relations_deleted": 1}
    assert repo.kg_delete_status(nb)["status"] == "succeeded"
    # Registered residual: the old root stays until a later refresh succeeds.
    assert (live / "manifest.json").exists()

    def _broken(_notebook_id):
        raise RuntimeError("preview store unavailable")

    monkeypatch.setattr(scale, "refresh_viz_after_graph_reset", _broken)
    _run_delete(repo, nb)
    assert repo.kg_delete_status(nb)["status"] == "succeeded"
    assert repo._runtime.knowledge_lifecycle.kg_maintenance.active_kind(nb) is None


# ---------------------------------------------------------------------------
# Job-history clearing (SQLite store; the PostgreSQL twin is pinned in
# tests/postgres/test_core_store_conformance.py)
# ---------------------------------------------------------------------------


def test_clear_terminal_jobs_never_touches_a_running_row(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    jobs = repo._runtime.kg_build_jobs
    finished_id = _finished_build_job(repo, nb)
    running = jobs.create_job(nb, "", "rebuild", 0)

    assert jobs.clear_terminal_jobs(nb) == 1
    assert jobs.clear_terminal_jobs(nb) == 0
    assert jobs.get(finished_id)["stage"] == "cleared"
    assert jobs.get(finished_id)["status"] == "succeeded"
    still_running = jobs.get(running["id"])
    assert still_running["status"] == "running"
    assert still_running["stage"] == "probing"
    # A running analysis keeps being reported, and settles normally.
    assert repo.get_notebook(nb).kg_build.job_id == running["id"]
    assert jobs.finish(running["id"], "succeeded")
    visible = repo.get_notebook(nb).kg_build
    assert visible.job_id == running["id"] and visible.stage == "finished"
