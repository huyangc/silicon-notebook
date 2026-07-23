"""P2·T4: 体检修复端点 —— 批量 reparse(H2/H3)+ 补齐向量(H4/H5)。

只验证端点接线与作用域守卫:后台 submit_job 被打桩记录(不真跑管线),断言排入了什么、
越权/越库被挡。真实的解析/嵌入由既有管线测试覆盖。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

_NOW = "2026-01-01T00:00:00"


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'repair.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")

    from app.api import deps
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    return TestClient(create_app())


def _register(client: TestClient, username: str) -> tuple[dict[str, str], str]:
    r = client.post("/api/auth/register", json={"username": username, "password": "pw"})
    assert r.status_code == 200, r.text
    body = r.json()
    return {"Authorization": f"Bearer {body['token']}"}, body["user"]["id"]


def _notebook(client: TestClient, headers: dict[str, str], name: str) -> str:
    r = client.post("/api/notebooks", headers=headers, json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _seed_source(repo, notebook_id: str, sid: str) -> None:
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "created_at,updated_at) VALUES (?,?,?,?,'extracted','extracted',?,?)",
            (sid, notebook_id, sid, "document", _NOW, _NOW),
        )


def _spy_submit_job(monkeypatch):
    """打桩 kg_scheduler.submit_job,记录 (fn, args) 而不真跑后台。"""
    calls = []

    def _spy(fn, /, *args, **kwargs):
        calls.append((fn, args))

    monkeypatch.setattr("app.services.kg.scheduler.submit_job", _spy)
    return calls


def test_reparse_schedules_only_in_notebook_sources(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    headers, _ = _register(client, "a00300301")
    nb = _notebook(client, headers, "target")
    other = _notebook(client, headers, "other")

    from app.api.deps import repository

    repo = repository()
    _seed_source(repo, nb, "src-mine")
    _seed_source(repo, other, "src-other")  # 属于别的 notebook

    calls = _spy_submit_job(monkeypatch)
    r = client.post(
        f"/api/notebooks/{nb}/sources/reparse",
        headers=headers,
        json={"source_ids": ["src-mine", "src-other", "src-missing"]},
    )
    assert r.status_code == 200, r.text
    # 只有真属于本 notebook 的 src-mine 被排入(越库的 src-other / 不存在的 src-missing 静默跳过)。
    assert r.json()["scheduled"] == ["src-mine"]
    assert len(calls) == 1
    fn, args = calls[0]
    assert args == ("src-mine",)
    assert getattr(fn, "__name__", "") == "process_source"


def test_reparse_dedupes_and_bounds(tmp_path, monkeypatch):
    """去重 + 限量(codex):重复 id 只排一次;超上限直接 400,一个都不排。"""
    client = _client(tmp_path, monkeypatch)
    headers, _ = _register(client, "e00300305")
    nb = _notebook(client, headers, "dedup")

    from app.api.deps import repository
    from app.api import source_routes

    _seed_source(repository(), nb, "src-dup")

    calls = _spy_submit_job(monkeypatch)
    dup = client.post(
        f"/api/notebooks/{nb}/sources/reparse",
        headers=headers,
        json={"source_ids": ["src-dup", "src-dup", "src-dup"]},
    )
    assert dup.status_code == 200, dup.text
    assert dup.json()["scheduled"] == ["src-dup"]  # 去重:只排一次
    assert len(calls) == 1

    over = client.post(
        f"/api/notebooks/{nb}/sources/reparse",
        headers=headers,
        json={"source_ids": [f"s{i}" for i in range(source_routes._REPARSE_MAX + 1)]},
    )
    assert over.status_code == 400  # 超上限拒绝
    assert len(calls) == 1  # 一个都没多排


def test_backfill_vectors_accepts_and_schedules(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    headers, _ = _register(client, "b00300302")
    nb = _notebook(client, headers, "bf")

    calls = _spy_submit_job(monkeypatch)
    r = client.post(f"/api/notebooks/{nb}/backfill-vectors", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] is True
    assert len(calls) == 1
    fn, args = calls[0]
    assert getattr(fn, "__name__", "") == "_backfill_vectors_job"
    assert args[1] == nb  # (repo, notebook_id)


def test_repair_endpoints_reject_stranger(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_headers, _ = _register(client, "c00300303")
    stranger_headers, _ = _register(client, "d00300304")
    nb = _notebook(client, owner_headers, "private")

    _spy_submit_job(monkeypatch)
    reparse = client.post(
        f"/api/notebooks/{nb}/sources/reparse",
        headers=stranger_headers,
        json={"source_ids": ["x"]},
    )
    backfill = client.post(
        f"/api/notebooks/{nb}/backfill-vectors", headers=stranger_headers
    )
    assert reparse.status_code == 404
    assert backfill.status_code == 404
