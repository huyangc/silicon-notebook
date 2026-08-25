"""P2·T4: 体检修复端点 —— 批量 reparse(H2/H3)+ 补齐向量(H4/H5)。

只验证端点接线与作用域守卫:后台 submit_job 被打桩记录(不真跑管线),断言排入了什么、
越权/越库被挡。真实的解析/嵌入由既有管线测试覆盖。
"""
from __future__ import annotations

import pytest
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


def _seed_source_with_elements(repo, notebook_id: str, sid: str, n: int = 2) -> None:
    """种 source + n 个非空 element(无 embedding)→ count_missing_element_vectors == n
    (H5 缺 element 向量的损坏,给 backfill 受理判定造真实待补量)。"""
    _seed_source(repo, notebook_id, sid)
    with repo._write() as db:
        for i in range(1, n + 1):
            db.execute(
                "INSERT INTO source_elements "
                "(id,source_id,element_type,location_label,text,metadata,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"el-{sid}-{i:04d}", sid, "paragraph", f"p{i}", f"text {i}", "{}", _NOW),
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


def test_reparse_refuses_pending_and_missing_pipeline_before_submit(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    headers, _ = _register(client, "p00300309")
    nb = _notebook(client, headers, "pipeline admission")

    from app.api.deps import repository

    repo = repository()
    _seed_source(repo, nb, "src-gated")
    calls = _spy_submit_job(monkeypatch)

    with repo._write() as db:
        db.execute(
            "UPDATE notebooks SET indexing_pipeline_job_id=? WHERE id=?",
            ("pending:generation", nb),
        )
    pending = client.post(
        f"/api/notebooks/{nb}/sources/reparse",
        headers=headers,
        json={"source_ids": ["src-gated"]},
    )
    assert pending.status_code == 409
    assert calls == []

    with repo._write() as db:
        db.execute(
            "UPDATE notebooks SET indexing_pipeline=?,"
            "indexing_pipeline_version=?,indexing_pipeline_job_id='' WHERE id=?",
            ("removed.pipeline", "v1", nb),
        )
    missing = client.post(
        f"/api/notebooks/{nb}/sources/reparse",
        headers=headers,
        json={"source_ids": ["src-gated"]},
    )
    assert missing.status_code == 409
    assert calls == []


def test_backfill_refuses_pending_pipeline_before_submit(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    headers, _ = _register(client, "v00300310")
    nb = _notebook(client, headers, "backfill admission")

    from app.api.deps import repository

    repo = repository()
    _seed_source_with_elements(repo, nb, "src-vector-gated")
    monkeypatch.setattr(repo, "configured", lambda _workload: True)
    calls = _spy_submit_job(monkeypatch)
    with repo._write() as db:
        db.execute(
            "UPDATE notebooks SET indexing_pipeline_job_id=? WHERE id=?",
            ("pending:generation", nb),
        )

    response = client.post(
        f"/api/notebooks/{nb}/backfill-vectors", headers=headers
    )
    assert response.status_code == 409
    assert calls == []

    from app.api.source_routes import _backfill_vectors_job
    from app.domain.indexing_pipeline import IndexingPipelineUnavailableError

    with pytest.raises(IndexingPipelineUnavailableError):
        _backfill_vectors_job(repo, nb)


def test_backfill_vectors_accepts_and_schedules(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    headers, _ = _register(client, "b00300302")
    nb = _notebook(client, headers, "bf")

    from app.api.deps import repository

    _seed_source_with_elements(repository(), nb, "src-bf")  # H5 缺 element 向量的损坏
    monkeypatch.setattr(repository(), "configured", lambda wid: True)  # 有嵌入服务
    calls = _spy_submit_job(monkeypatch)
    r = client.post(f"/api/notebooks/{nb}/backfill-vectors", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] is True
    assert len(calls) == 1
    fn, args = calls[0]
    assert getattr(fn, "__name__", "") == "_backfill_vectors_job"
    assert args[1] == nb  # (repo, notebook_id)


def test_backfill_vectors_rejects_when_embedding_unconfigured(tmp_path, monkeypatch):
    """两个嵌入 workload 都没配 → backfill 会完全 no-op → 不假受理(codex):accepted=false、
    不排 job。否则前端说「已开始」空转轮询、H4/H5 永不清。"""
    client = _client(tmp_path, monkeypatch)
    headers, _ = _register(client, "f00300306")
    nb = _notebook(client, headers, "bf-unconf")

    from app.api.deps import repository

    monkeypatch.setattr(repository(), "configured", lambda wid: False)  # 未配嵌入
    calls = _spy_submit_job(monkeypatch)
    r = client.post(f"/api/notebooks/{nb}/backfill-vectors", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] is False
    assert len(calls) == 0  # 未配 → 一个 job 都不排


def test_backfill_vectors_rejects_when_damage_workload_unconfigured(tmp_path, monkeypatch):
    """损坏是 element 向量(H5)、却只配了 chunk 嵌入 → 该类补不动 → 不假受理(codex P2:按损坏
    所属 workload 精判;粗判 `configured(chunk) or configured(element)` 会假受理→前端「修复中」
    空转轮询、而 H5 永不清)。"""
    client = _client(tmp_path, monkeypatch)
    headers, _ = _register(client, "h00300308")
    nb = _notebook(client, headers, "bf-mismatch")

    from app.api.deps import repository

    _seed_source_with_elements(repository(), nb, "src-el")  # 只有 element 缺向量、无 chunk
    monkeypatch.setattr(
        repository(), "configured", lambda wid: wid == "chunk_embedding"  # 只配 chunk
    )
    calls = _spy_submit_job(monkeypatch)
    r = client.post(f"/api/notebooks/{nb}/backfill-vectors", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] is False  # 缺的是 element、element 未配 → 不受理
    assert len(calls) == 0


def test_backfill_vectors_accepts_when_matching_workload_configured(tmp_path, monkeypatch):
    """损坏是 element 向量、且 element 嵌入已配 → 受理并排 job(即便 chunk 未配:各类独立判,
    有一类「已配 + 有缺」即受理)。"""
    client = _client(tmp_path, monkeypatch)
    headers, _ = _register(client, "i00300309")
    nb = _notebook(client, headers, "bf-match")

    from app.api.deps import repository

    _seed_source_with_elements(repository(), nb, "src-el2")
    monkeypatch.setattr(
        repository(), "configured", lambda wid: wid == "source_element_embedding"
    )
    calls = _spy_submit_job(monkeypatch)
    r = client.post(f"/api/notebooks/{nb}/backfill-vectors", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] is True
    assert len(calls) == 1


def test_reparse_rejects_hidden_projection_source(tmp_path, monkeypatch):
    """memory/knowhow 隐藏合成源无 file_path、由投影服务维护——即便属于本 notebook,也不能喂给
    文档解析 process_source(会标失败/清派生态)。owner 从引用数据带来的隐藏源 id 静默跳过(codex)。"""
    client = _client(tmp_path, monkeypatch)
    headers, _ = _register(client, "g00300307")
    nb = _notebook(client, headers, "hidden")

    from app.api.deps import repository

    repo = repository()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "created_at,updated_at) VALUES ('src-kh',?,?,'knowhow','ready','ready',?,?)",
            (nb, "hidden", _NOW, _NOW),
        )

    calls = _spy_submit_job(monkeypatch)
    r = client.post(
        f"/api/notebooks/{nb}/sources/reparse",
        headers=headers,
        json={"source_ids": ["src-kh"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["scheduled"] == []  # 隐藏源不排
    assert len(calls) == 0


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
