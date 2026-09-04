from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'api.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "true")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    from app.core.config import get_settings
    from app.api import deps
    from app.extensions import default_extension_runtime

    get_settings.cache_clear()
    deps.repository.cache_clear()
    default_extension_runtime.cache_clear()
    from app.main import create_app

    return TestClient(create_app())


def _notebook(client: TestClient) -> str:
    response = client.post("/api/notebooks", json={"name": "pipeline api"})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_reader_projection_is_sanitized_and_builtin_is_selected(client):
    notebook_id = _notebook(client)

    response = client.get(f"/api/notebooks/{notebook_id}/indexing-pipeline")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "pipeline_id",
        "version",
        "available",
        "missing",
        "pending",
        "large_library_locked",
        "options",
        "changed",
        "warning_count",
        "rebuild_status",
        "job_id",
    }
    assert body["pipeline_id"] is None
    assert body["available"] is True
    assert body["missing"] is False
    assert body["options"][0]["label"] == "内建管线"
    serialized = response.text.lower()
    for forbidden in ("reason", "exception", "endpoint", "credential", "path"):
        assert forbidden not in serialized
    summary = client.get(f"/api/notebooks/{notebook_id}").json()
    assert summary["indexing_pipeline_id"] is None
    assert summary["indexing_pipeline_version"] == "builtin.chunk.v1"
    assert summary["indexing_pipeline_available"] is True
    assert summary["indexing_pipeline_missing"] is False
    assert summary["indexing_pipeline_pending"] is False
    assert summary["indexing_pipeline_stale"] is False


def test_patch_null_is_idempotent_and_does_not_create_a_job(client):
    notebook_id = _notebook(client)

    response = client.patch(
        f"/api/notebooks/{notebook_id}/indexing-pipeline",
        json={"pipeline_id": None},
    )

    assert response.status_code == 200
    assert response.json()["changed"] is False
    assert response.json()["pending"] is False
    assert response.json()["job_id"] is None


def test_missing_selected_plugin_keeps_get_readable_and_new_index_write_is_409(
    client,
):
    notebook_id = _notebook(client)
    from app.api.deps import repository

    repo = repository()
    with repo._write() as db:
        db.execute(
            "UPDATE notebooks SET indexing_pipeline=?,"
            "indexing_pipeline_version=?,indexing_pipeline_generation=?,"
            "indexing_pipeline_job_id='' WHERE id=?",
            ("removed.pipeline", "v7", "old-generation", notebook_id),
        )
        db.execute(
            "INSERT INTO unified_kg_state "
            "(notebook_id,dirty,updated_at,indexing_pipeline_id,"
            "indexing_pipeline_version) VALUES (?,?,?,?,?) "
            "ON CONFLICT(notebook_id) DO UPDATE SET "
            "indexing_pipeline_id=excluded.indexing_pipeline_id,"
            "indexing_pipeline_version=excluded.indexing_pipeline_version",
            (notebook_id, 0, repo._runtime.seams.now(), "removed.pipeline", "v7"),
        )

    readable = client.get(f"/api/notebooks/{notebook_id}/indexing-pipeline")
    blocked = client.post(
        f"/api/notebooks/{notebook_id}/scale-index/rebuild",
        json={"when": "now", "mode": "full"},
    )

    assert readable.status_code == 200
    assert readable.json()["missing"] is True
    assert readable.json()["available"] is False
    assert blocked.status_code == 409
    assert blocked.headers["X-User-Message"] == "1"
    assert "旧索引仍可读取" in blocked.json()["detail"]
    summary = client.get(f"/api/notebooks/{notebook_id}").json()
    assert summary["indexing_pipeline_id"] == "removed.pipeline"
    assert summary["indexing_pipeline_version"] == "v7"
    assert summary["indexing_pipeline_available"] is False
    assert summary["indexing_pipeline_missing"] is True
    assert summary["indexing_pipeline_pending"] is False
    assert summary["indexing_pipeline_stale"] is False


def test_switch_is_refused_while_a_rebuild_worker_is_active(client):
    """活跃 rebuild 期间的再次切换必须在改 desired 之前被拒绝。

    旧行为先铸新 generation 再撞 KgBuildAlreadyRunning:正在跑的 worker 花完整库
    模型/embedding 开销后输掉 publish CAS(整轮作废),而提交者只读到一句无害 409,
    且 job authority 永远停在 pending:<新代>。判据是「desired 列一个字没动」。
    """
    notebook_id = _notebook(client)
    from app.api.deps import repository

    repo = repository()
    job = repo._runtime.kg_build_jobs.create_job(
        notebook_id, "active-rebuild", "rebuild", 0
    )
    with repo._write() as db:
        db.execute(
            "UPDATE notebooks SET indexing_pipeline_generation='g-active',"
            "indexing_pipeline_job_id=? WHERE id=?",
            (job["id"], notebook_id),
        )

    refused = client.patch(
        f"/api/notebooks/{notebook_id}/indexing-pipeline",
        json={"pipeline_id": None},
    )

    assert refused.status_code == 409
    assert refused.headers["X-User-Message"] == "1"
    assert "索引重建正在进行" in refused.json()["detail"]
    with repo._connect() as db:
        row = db.execute(
            "SELECT indexing_pipeline_generation,indexing_pipeline_job_id "
            "FROM notebooks WHERE id=?",
            (notebook_id,),
        ).fetchone()
    assert dict(row) == {
        "indexing_pipeline_generation": "g-active",
        "indexing_pipeline_job_id": job["id"],
    }


def _mark_large(monkeypatch) -> None:
    """把当前 repo 判成大库(批 3·W3 D3 判据的测试化身):活跃对象数超过
    INDEXING_PIPELINE_SWITCH_MAX_OBJECTS——runtime 侧单一判定点。"""
    from app.api import deps

    runtime = deps.repository()._runtime
    monkeypatch.setattr(
        runtime, "_pipeline_switch_locked", lambda notebook_id: True,
    )


def test_large_library_locks_the_pipeline_switch(client, monkeypatch):
    """批 3·W3(审计 WR-2/决策 D3):大库上任何**变更**都被 begin() 在改
    desired 之前拒绝(409 + 明确文案,什么都没保存);GET 投影带
    large_library_locked=True 供前端禁用控件;无变化的幂等保存仍是 200
    no-op(打开设置直接点保存不该报错)。"""
    notebook_id = _notebook(client)
    _mark_large(monkeypatch)
    # 本 harness 未加载部署插件——注入一个可用的假 option,让请求越过
    # 「所选管线不可用」的前置校验、真正走到大库闸(校验序刻意如此:
    # 无效 id 该报不可用,而不是大库文案)。
    from app.api import deps
    from app.domain.indexing_pipeline import IndexingPipelineOption

    service = deps.repository()._runtime.indexing_pipeline
    original_option = service._option
    fake = IndexingPipelineOption(
        pipeline_id="deploy.custom", label="x", description="",
        version="v1", overrides_chunking=True,
        overrides_kg_extraction=False, available=True,
    )
    monkeypatch.setattr(
        service, "_option",
        lambda pid: fake if pid == "deploy.custom" else original_option(pid),
    )

    projection = client.get(
        f"/api/notebooks/{notebook_id}/indexing-pipeline"
    ).json()
    assert projection["large_library_locked"] is True

    # 幂等保存(仍是内建、无变化)走「无变化早退」,先于大库闸——200。
    unchanged = client.patch(
        f"/api/notebooks/{notebook_id}/indexing-pipeline",
        json={"pipeline_id": None},
    )
    assert unchanged.status_code == 200
    assert unchanged.json()["rebuild_status"] == "idle"

    # 真变更被拒:409 + 文案;desired 未落库(投影回读仍是内建/idle)。
    refused = client.patch(
        f"/api/notebooks/{notebook_id}/indexing-pipeline",
        json={"pipeline_id": "deploy.custom"},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"] == (
        "这本笔记本规模较大，暂不支持切换索引管线；当前索引不受影响。"
    )
    after = client.get(
        f"/api/notebooks/{notebook_id}/indexing-pipeline"
    ).json()
    assert after["pipeline_id"] is None
    assert after["pending"] is False
    assert after["rebuild_status"] == "idle"


def test_large_library_still_allows_builtin_recovery(client, monkeypatch):
    """内评双 P1:卡在缺席/失败自定义管线上的大库,require_write_admission
    让全部写入 fail-closed——「切回内建」是唯一自助出口,必须豁免大库闸,
    否则笔记本永久写锁死。"""
    notebook_id = _notebook(client)
    from app.api import deps

    runtime = deps.repository()._runtime
    # 先造出「desired 停在自定义管线」的卡死态(begin 落 desired 后 worker
    # 炸掉留下的形态),再判成大库。
    runtime.notebook_store.set_indexing_pipeline_desired(
        notebook_id, "deploy.custom", "v1")
    _mark_large(monkeypatch)
    stuck = client.get(
        f"/api/notebooks/{notebook_id}/indexing-pipeline"
    ).json()
    assert stuck["large_library_locked"] is True
    assert stuck["pending"] is True

    recovered = client.patch(
        f"/api/notebooks/{notebook_id}/indexing-pipeline",
        json={"pipeline_id": None},
    )
    assert recovered.status_code == 200, recovered.text
    # 重试同一条自定义管线(非内建目标)仍被闸。
    retried = client.patch(
        f"/api/notebooks/{notebook_id}/indexing-pipeline",
        json={"pipeline_id": "deploy.custom"},
    )
    assert retried.status_code in (409,), retried.text


def test_small_library_projection_is_not_locked(client):
    notebook_id = _notebook(client)
    projection = client.get(
        f"/api/notebooks/{notebook_id}/indexing-pipeline"
    ).json()
    assert projection["large_library_locked"] is False
