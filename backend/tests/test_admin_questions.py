from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'questions.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    from app.api import deps
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    return TestClient(create_app())


def _register(client: TestClient, username: str) -> tuple[dict[str, str], str]:
    response = client.post(
        "/api/auth/register", json={"username": username, "password": "pw"}
    )
    assert response.status_code == 200
    body = response.json()
    return {"Authorization": f"Bearer {body['token']}"}, body["user"]["id"]


def _admin(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin"}
    )
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_admin_questions_combines_ask_and_report_with_filters(client):
    user_headers, user_id = _register(client, "d00000004")
    notebook = client.post(
        "/api/notebooks", headers=user_headers, json={"name": "模拟电路"}
    ).json()
    from app.api.deps import repository

    now = "2026-08-31T08:00:00+00:00"
    full_ask_question = "如何降低噪声？这是历史已完成提问的尾部检索词"
    with repository()._write() as db:
        db.execute(
            "INSERT INTO answers(id,notebook_id,question,payload,created_at) "
            "VALUES (?,?,?,?,?)",
            ("answer-global", notebook["id"], full_ask_question, "{}", now),
        )
        db.execute(
            "INSERT INTO ask_jobs(id,notebook_id,conversation_id,created_by,mode,question,status,answer_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("ask-global", notebook["id"], "", user_id, "chunk", "如何降低噪声？", "completed", "answer-global", now, now),
        )
        db.execute(
            "INSERT INTO reports(id,notebook_id,question,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("report-global", notebook["id"], "分析放大器稳定性", user_id, "done", now, now),
        )

    assert client.get("/api/admin/questions", headers=user_headers).status_code == 403
    admin = _admin(client)
    response = client.get("/api/admin/questions", headers=admin)
    assert response.status_code == 200
    body = response.json()
    assert body["stats"] == {
        "total": 2, "asks": 1, "reports": 1, "active_users": 1, "global_asks": 0,
    }
    assert {item["type"] for item in body["items"]} == {"ask", "report"}
    assert all(item["username"] == "d00000004" for item in body["items"])
    assert all(item["notebook_name"] == "模拟电路" for item in body["items"])
    ask_item = next(item for item in body["items"] if item["type"] == "ask")
    assert ask_item["question"] == full_ask_question

    ask_tail = client.get("/api/admin/questions?kind=ask&q=尾部检索词", headers=admin).json()
    assert ask_tail["total"] == 1
    assert ask_tail["items"][0]["question"] == full_ask_question

    filtered = client.get("/api/admin/questions?kind=report&q=稳定", headers=admin).json()
    assert filtered["total"] == 1
    assert filtered["stats"] == {
        "total": 1, "asks": 0, "reports": 1, "active_users": 1, "global_asks": 0,
    }
    assert filtered["items"][0]["question"] == "分析放大器稳定性"

    # 批 3·W1 PR-3:DELETE 现在是 202(tombstone CAS 立即返回),实际归档由
    # 后台删除作业完成——drain 等它跑完,下面的断言才看得到 retained_user_activity。
    assert client.delete(f"/api/notebooks/{notebook['id']}", headers=user_headers).status_code == 202
    from app.services import background_jobs
    background_jobs._drain_maintenance_executors_for_tests(timeout=10.0)
    retained = client.get("/api/admin/questions", headers=admin).json()
    assert retained["stats"] == {
        "total": 2, "asks": 1, "reports": 1, "active_users": 1, "global_asks": 0,
    }
    assert {item["id"] for item in retained["items"]} == {"ask-global", "report-global"}
    assert all(item["notebook_name"] == "模拟电路" for item in retained["items"])
    retained_ask = next(item for item in retained["items"] if item["type"] == "ask")
    assert retained_ask["question"] == full_ask_question

    assert client.get(
        f"/api/admin/questions?q={'😀' * 200}&limit=200", headers=admin
    ).status_code == 200
    assert client.get(
        f"/api/admin/questions?q={'😀' * 201}", headers=admin
    ).status_code == 422
    assert client.get("/api/admin/questions?limit=201", headers=admin).status_code == 422
    # 未过滤时每条 item 都带 submitted_via;两条历史行都没写这一列 -> 默认 ""。
    assert all(item["submitted_via"] == "" for item in body["items"])


def test_admin_questions_filters_by_submitted_via(client):
    """submitted_via 过滤 web/mcp 生效、随过滤变的 stats、非法值 422、笔记本
    删除后 retained 行保留原值(不因迁移到 retained_user_activity 而丢失)。"""
    user_headers, user_id = _register(client, "d00000005")
    notebook = client.post(
        "/api/notebooks", headers=user_headers, json={"name": "调用方式"}
    ).json()
    from app.api.deps import repository

    now = "2026-09-15T08:00:00+00:00"
    with repository()._write() as db:
        db.execute(
            "INSERT INTO ask_jobs(id,notebook_id,conversation_id,created_by,mode,"
            "question,status,created_at,updated_at,submitted_via) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("ask-web", notebook["id"], "", user_id, "chunk",
             "网页提交的问题", "completed", now, now, "web"),
        )
        db.execute(
            "INSERT INTO ask_jobs(id,notebook_id,conversation_id,created_by,mode,"
            "question,status,created_at,updated_at,submitted_via) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("ask-mcp", notebook["id"], "", user_id, "chunk",
             "MCP 提交的问题", "completed", now, now, "mcp"),
        )
        db.execute(
            "INSERT INTO reports(id,notebook_id,question,created_by,status,"
            "created_at,updated_at,submitted_via) VALUES (?,?,?,?,?,?,?,?)",
            ("report-mcp", notebook["id"], "MCP 提交的报告", user_id, "done",
             now, now, "mcp"),
        )

    admin = _admin(client)

    web_only = client.get(
        "/api/admin/questions?submitted_via=web", headers=admin
    ).json()
    assert web_only["stats"] == {
        "total": 1, "asks": 1, "reports": 0, "active_users": 1, "global_asks": 0,
    }
    assert [item["id"] for item in web_only["items"]] == ["ask-web"]
    assert web_only["items"][0]["submitted_via"] == "web"

    mcp_only = client.get(
        "/api/admin/questions?submitted_via=mcp", headers=admin
    ).json()
    assert mcp_only["stats"] == {
        "total": 2, "asks": 1, "reports": 1, "active_users": 1, "global_asks": 0,
    }
    assert {item["id"] for item in mcp_only["items"]} == {"ask-mcp", "report-mcp"}
    assert all(item["submitted_via"] == "mcp" for item in mcp_only["items"])

    assert client.get(
        "/api/admin/questions?submitted_via=bogus", headers=admin
    ).status_code == 422

    # 笔记本删除后,retained_user_activity 行必须保留原 submitted_via,不能
    # 在迁移到保留投影时丢失或被清空。
    assert client.delete(
        f"/api/notebooks/{notebook['id']}", headers=user_headers
    ).status_code == 202
    from app.services import background_jobs
    background_jobs._drain_maintenance_executors_for_tests(timeout=10.0)
    retained_mcp = client.get(
        "/api/admin/questions?submitted_via=mcp", headers=admin
    ).json()
    assert {item["id"] for item in retained_mcp["items"]} == {"ask-mcp", "report-mcp"}
    assert all(item["submitted_via"] == "mcp" for item in retained_mcp["items"])
    retained_web = client.get(
        "/api/admin/questions?submitted_via=web", headers=admin
    ).json()
    assert [item["id"] for item in retained_web["items"]] == ["ask-web"]
    assert retained_web["items"][0]["submitted_via"] == "web"


def test_admin_questions_includes_global_ask_jobs(client):
    """一条全局问答(global_ask_conversations + global_ask_jobs)在总览里现身:
    type=ask、scope=global、notebook_id/notebook_name 为空、submitted_via 来自
    列。stats.asks 把它计入,并且 stats.global_asks 单独计数一次。scope 过滤
    与 submitted_via 过滤、q 全文搜索、kind=report 排除都要覆盖到。"""
    user_headers, user_id = _register(client, "d00000006")
    from app.api.deps import repository

    now = "2026-09-16T08:00:00+00:00"
    question = "跨库比较两个电路的噪声"
    with repository()._write() as db:
        db.execute(
            "INSERT INTO global_ask_conversations"
            "(id,user_id,title,scope_json,submitted_via,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("gconv-1", user_id, "", '{"mode":"all","notebook_ids":[]}', "mcp", now, now),
        )
        db.execute(
            "INSERT INTO global_ask_jobs"
            "(id,conversation_id,user_id,client_request_id,request_json,status,"
            "payload_json,created_at,submitted_via,asked_at,updated_at,error_detail) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("gask-1", "gconv-1", user_id, None, "{}", "done",
             f'{{"question":"{question}"}}', now, "mcp", "", now, ""),
        )

    admin = _admin(client)
    response = client.get("/api/admin/questions", headers=admin)
    assert response.status_code == 200
    body = response.json()
    global_item = next(item for item in body["items"] if item["id"] == "gask-1")
    assert global_item["type"] == "ask"
    assert global_item["scope"] == "global"
    assert global_item["notebook_id"] == ""
    assert global_item["notebook_name"] == ""
    assert global_item["submitted_via"] == "mcp"
    assert global_item["question"] == question
    assert body["stats"]["asks"] == 1
    assert body["stats"]["global_asks"] == 1

    scoped_global = client.get(
        "/api/admin/questions?scope=global", headers=admin
    ).json()
    assert {item["id"] for item in scoped_global["items"]} == {"gask-1"}
    assert scoped_global["stats"]["global_asks"] == 1

    scoped_notebook = client.get(
        "/api/admin/questions?scope=notebook", headers=admin
    ).json()
    assert "gask-1" not in {item["id"] for item in scoped_notebook["items"]}
    assert scoped_notebook["stats"]["global_asks"] == 0

    mcp_filtered = client.get(
        "/api/admin/questions?submitted_via=mcp", headers=admin
    ).json()
    assert "gask-1" in {item["id"] for item in mcp_filtered["items"]}

    q_filtered = client.get(
        "/api/admin/questions?q=跨库", headers=admin
    ).json()
    assert {item["id"] for item in q_filtered["items"]} == {"gask-1"}

    report_only = client.get(
        "/api/admin/questions?kind=report", headers=admin
    ).json()
    assert "gask-1" not in {item["id"] for item in report_only["items"]}
