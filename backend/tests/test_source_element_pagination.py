"""Source detail reads bounded element windows, including citation anchors."""
from __future__ import annotations

from fastapi.testclient import TestClient


_NOW = "2026-01-01T00:00:00"


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'elements-page.db'}")
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


def _register(client: TestClient, username: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/register", json={"username": username, "password": "pw"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _seed_source(repo, notebook_id: str, source_id: str, count: int) -> None:
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,'extracted','extracted',?,?)",
            (source_id, notebook_id, "Large source", "document", _NOW, _NOW),
        )
        db.executemany(
            "INSERT INTO source_elements "
            "(id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (
                    f"element-{index:04d}",
                    source_id,
                    "paragraph",
                    f"Page {index}",
                    f"text {index}",
                    "{}",
                    _NOW,
                )
                for index in range(count)
            ],
        )


def test_scoped_element_page_is_bounded_and_resolves_anchor(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner = _register(client, "a00900001")
    notebook = client.post(
        "/api/notebooks", headers=owner, json={"name": "Paging"}
    ).json()["id"]

    from app.api.deps import repository

    _seed_source(repository(), notebook, "source-large", 85)

    first = client.get(
        f"/api/notebooks/{notebook}/sources/source-large/elements-page",
        headers=owner,
    )
    assert first.status_code == 200, first.text
    assert first.json()["total_count"] == 85
    assert first.json()["offset"] == 0
    assert len(first.json()["items"]) == 40
    assert [item["id"] for item in first.json()["items"][:2]] == [
        "element-0000",
        "element-0001",
    ]

    anchored = client.get(
        f"/api/notebooks/{notebook}/sources/source-large/elements-page",
        headers=owner,
        params={"anchor_element_id": "element-0060"},
    )
    assert anchored.status_code == 200, anchored.text
    assert anchored.json()["offset"] == 40
    assert len(anchored.json()["items"]) == 40
    assert "element-0060" in [item["id"] for item in anchored.json()["items"]]


def test_element_page_keeps_source_read_authorization(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner = _register(client, "b00900002")
    stranger = _register(client, "c00900003")
    notebook = client.post(
        "/api/notebooks", headers=owner, json={"name": "Private"}
    ).json()["id"]

    from app.api.deps import repository

    _seed_source(repository(), notebook, "source-private", 1)
    response = client.get(
        "/api/sources/source-private/elements-page", headers=stranger
    )
    assert response.status_code == 404



# ---------------------------------------------------------------------------
# 生产热点整改批 E: source_elements_after —— 整源走查用的 keyset 分页读。
# 与 source_elements_page(详情窗口、OFFSET 型、limit 夹到 100)是两个东西:
# 这个不夹 limit、不数 total,且**全局顺序必须与 source_elements 逐位一致**,
# 否则整源流水线换了顺序就不是同一条走查。
# ---------------------------------------------------------------------------


def _repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'after.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository

    return SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'after.db'}",
            storage_dir=str(tmp_path / "storage"),
            event_log_enabled=False,
            llm_log_enabled=False,
            model_services_config="",
        )
    )


def _seed_source_multi_timestamp(repo, notebook_id: str, source_id: str, count: int):
    """元素跨多个 created_at,**且每个 created_at 下恰好三行** —— 让
    ``(created_at, id)`` 的次键真正参与,只按 created_at 推进的游标会在这里跳行
    或重复。(镜像 PG 侧的同名构造;第一版把秒数写成 ``index % 7`` 而天数写成
    ``index // 7``,于是每个 (天, 秒) 组合恰好唯一、一个 tie 都没有 —— 注释说的
    覆盖当时并不成立。)"""
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,'extracted','extracted',?,?)",
            (source_id, notebook_id, "S", "document", _NOW, _NOW),
        )
        db.executemany(
            "INSERT INTO source_elements "
            "(id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (
                    f"element-{source_id}-{index:04d}", source_id, "paragraph",
                    f"Page {index}", f"text {index}", "{}",
                    # 每 3 个元素共用一个 created_at → 真正的并列;
                    # 每 7 个时刻换一天,保证同时跨天。
                    f"2026-01-0{1 + index // 21}T00:00:0{(index // 3) % 7}",
                )
                for index in range(count)
            ],
        )


def test_source_elements_after_walk_equals_the_whole_source_read(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    from app.models.schemas import NotebookCreate

    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    _seed_source_multi_timestamp(repo, nb, "src-after", 41)
    store = repo._runtime.source_store

    whole = store.source_elements("src-after")
    for page_size in (1, 2, 7, 40, 41, 100):
        walked, after, pages = [], None, 0
        while True:
            items, after = store.source_elements_after("src-after", after, page_size)
            pages += 1
            assert len(items) <= page_size
            walked.extend(items)
            if after is None:
                break
            assert pages <= 60, "游标没有推进"
        assert walked == whole, page_size
        # 满页才发下一次查询:短页立刻收尾。
        assert after is None


def test_source_elements_after_empty_source_returns_no_cursor(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    from app.models.schemas import NotebookCreate

    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    _seed_source_multi_timestamp(repo, nb, "src-empty", 0)
    store = repo._runtime.source_store
    assert store.source_elements_after("src-empty", None, 10) == ([], None)


def test_source_elements_after_is_scoped_to_one_source(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    from app.models.schemas import NotebookCreate

    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    _seed_source_multi_timestamp(repo, nb, "src-a", 9)
    _seed_source_multi_timestamp(repo, nb, "src-b", 9)
    store = repo._runtime.source_store
    walked, after = [], None
    while True:
        items, after = store.source_elements_after("src-a", after, 4)
        walked.extend(items)
        if after is None:
            break
    assert {el.source_id for el in walked} == {"src-a"}
    assert len(walked) == 9
