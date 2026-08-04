"""API-layer tests for the「活动」view's admin endpoints (P1 task 2):

  GET /admin/users/{user_id}/activity
  GET /admin/users/{user_id}/notebooks/{notebook_id}/sources
  GET /admin/users/{user_id}/asks/{job_id}

These exercise the full HTTP stack (auth, permission gate, response-model
field names) — the repository-layer merge/pagination semantics of
``list_user_activity`` itself are already covered by
``test_admin_user_activity.py``. See
docs/superpowers/specs/2026-08-04-user-activity-log-view-design_zh.md §4.2
and frontend/app/dev/logs/activity/types.ts for the frozen field contract.
"""
import json

import pytest
from fastapi.testclient import TestClient


def _make_client(tmp_path, monkeypatch, *, debug_logs: bool = True):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    # 活动视图与「模型调用」视图共用这一个部署开关(见 admin_routes.
    # _require_activity_enabled 的 docstring)。默认关闭,所以取数用例必须显式打开
    # ——这不是夹具样板,而是让「关掉时到底还能不能读到别人的问答」成为一条可断言
    # 的事实(见 test_activity_endpoints_404_when_debug_logs_disabled)。
    monkeypatch.setenv("DEBUG_LOGS_ENABLED", "1" if debug_logs else "0")
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.api import deps
    deps.repository.cache_clear()
    from app.main import create_app
    return TestClient(create_app())


@pytest.fixture
def client(tmp_path, monkeypatch):
    return _make_client(tmp_path, monkeypatch)


def _repo():
    from app.api import deps
    return deps.repository()


def _username(n: int) -> str:
    # Must match the seeded username shape ^[a-z]00\\d{6}$ (see
    # test_admin_user_notebooks.py's "z00123456" precedent).
    return f"z00{n:06d}"


def _auth(client, n: int):
    username = _username(n)
    client.post("/api/auth/register", json={"username": username, "password": "pw"})
    t = client.post("/api/auth/login", json={"username": username, "password": "pw"}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def _auth_admin(client):
    t = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def _me(client, headers) -> str:
    return client.get("/api/me", headers=headers).json()["id"]


def _create_notebook(client, headers, name="NB") -> str:
    return client.post("/api/notebooks", json={"name": name}, headers=headers).json()["id"]


def _insert_source(db, source_id, notebook_id, created_at, *,
                    title="Doc", source_type="pdf", status="parsed",
                    parse_status="parsed", file_name="doc.pdf",
                    error_message="") -> None:
    db.execute(
        "INSERT INTO sources "
        "(id,notebook_id,title,source_type,status,parse_status,file_name,"
        "error_message,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (source_id, notebook_id, title, source_type, status, parse_status,
         file_name, error_message, created_at, created_at),
    )


def _insert_paper_meta(db, source_id, notebook_id, paper_title, *, is_paper=1) -> None:
    db.execute(
        "INSERT INTO source_paper_meta "
        "(source_id,notebook_id,is_paper,paper_title,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (source_id, notebook_id, is_paper, paper_title,
         "2026-08-01T00:00:00", "2026-08-01T00:00:00"),
    )


def _insert_ask_job(db, job_id, notebook_id, created_by, created_at, *,
                     conversation_id="", question="q?", mode="chunk",
                     status="completed", asked_at="", answer_id="", error="") -> None:
    db.execute(
        "INSERT INTO ask_jobs "
        "(id,notebook_id,conversation_id,created_by,mode,question,status,asked_at,"
        "answer_id,error,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, notebook_id, conversation_id, created_by, mode, question, status,
         asked_at, answer_id, error, created_at, created_at),
    )


def _insert_conversation(db, conversation_id, notebook_id, created_by, updated_at) -> None:
    db.execute(
        "INSERT INTO conversations (id,notebook_id,title,created_by,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (conversation_id, notebook_id, "", created_by, updated_at, updated_at),
    )


def _insert_answer(db, answer_id, notebook_id, conversation_id, question, payload,
                    created_at) -> None:
    db.execute(
        "INSERT INTO answers (id,notebook_id,question,payload,created_at,conversation_id) "
        "VALUES (?,?,?,?,?,?)",
        (answer_id, notebook_id, question, json.dumps(payload), created_at,
         conversation_id),
    )


# --- GET /admin/users/{user_id}/activity ------------------------------------


def test_activity_forbidden_for_other_regular_user(client):
    a = _auth(client, 1)
    b = _auth(client, 2)
    uid_b = _me(client, b)
    resp = client.get(f"/api/admin/users/{uid_b}/activity", headers=a)
    assert resp.status_code == 403


def test_activity_allowed_for_self(client):
    a = _auth(client, 3)
    uid_a = _me(client, a)
    resp = client.get(f"/api/admin/users/{uid_a}/activity", headers=a)
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "has_more": False, "next_cursor": None}


def test_activity_allowed_for_admin_any_user(client):
    admin = _auth_admin(client)
    a = _auth(client, 4)
    uid_a = _me(client, a)
    resp = client.get(f"/api/admin/users/{uid_a}/activity", headers=admin)
    assert resp.status_code == 200


def test_activity_source_display_title_prefers_paper_title(client):
    """The one test the task letter calls out by name: a paper source must
    show its parsed paper title in the activity feed, not the uploaded file
    name — this is the display_title synthesis this task adds."""
    a = _auth(client, 5)
    uid_a = _me(client, a)
    nb_id = _create_notebook(client, a, "NB-activity")
    with _repo()._write() as db:
        _insert_source(db, "src-1", nb_id, "2026-08-01T10:00:00",
                        title="uploaded-name.pdf", file_name="1706.03762.pdf")
        _insert_paper_meta(db, "src-1", nb_id, "Attention Is All You Need")

    resp = client.get(f"/api/admin/users/{uid_a}/activity", headers=a)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["type"] == "source"
    assert item["id"] == "src-1"
    assert item["notebook_id"] == nb_id
    # The load-bearing assertion: paper title wins over the uploaded title.
    assert item["display_title"] == "Attention Is All You Need"
    assert item["file_name"] == "1706.03762.pdf"
    assert item["source_type"] == "pdf"
    assert item["parse_status"] == "parsed"
    assert item["paper_meta_status"] == "has_meta"
    assert item["parse_quality_warning"] is False
    assert item["extraction_warning"] == ""
    # 原始 error_message 绝不出库:它是 str(exc) 原样落库,可能带服务端绝对
    # 路径,而这个端点的读者可能是正在看别人数据的 admin。只暴露派生布尔。
    assert "error_message" not in item
    assert item["parse_failed"] is False
    # display_title must be a top-level field — the raw title/paper_title/
    # is_paper columns are not part of the frozen frontend contract.
    assert "title" not in item
    assert "paper_title" not in item
    assert "is_paper" not in item


def test_activity_source_display_title_falls_back_to_title(client):
    """A non-paper source keeps its ordinary source title (no meta row)."""
    a = _auth(client, 6)
    uid_a = _me(client, a)
    nb_id = _create_notebook(client, a, "NB-activity2")
    with _repo()._write() as db:
        _insert_source(db, "src-2", nb_id, "2026-08-01T10:00:00",
                        title="Plain Doc", source_type="md", file_name="notes.md")

    resp = client.get(f"/api/admin/users/{uid_a}/activity", headers=a)
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["display_title"] == "Plain Doc"


@pytest.mark.parametrize("field", ["since", "until", "before_ts"])
def test_activity_malformed_time_param_is_400_with_user_message(client, field):
    """畸形时间参数必须是 400 + 可上屏的中文文案,不是 500(F3②)。

    两个 store 对畸形边界都抛 ValueError(activity_time 的契约:不静默忽略)。路由层
    此前完全没接住它:``?since=not-a-date`` 实测是一条 **500 且不带 X-User-Message**,
    前端只能兜成「…请重试」,然后无限重试一个永远不会成功的请求。

    before_ts 一并覆盖:它是**服务端自己发出去**的游标,畸形游标同样不该 500。
    """
    a = _auth(client, 30)
    uid_a = _me(client, a)
    params = {field: "not-a-date"}
    if field == "before_ts":
        # 游标谓词要 ts 与 id 同时在场才生效,少一个就退回「无游标」而不触发解析。
        params["before_id"] = "whatever"

    resp = client.get(f"/api/admin/users/{uid_a}/activity", params=params, headers=a)
    assert resp.status_code == 400
    # 红线:后端中文用户文案必须打 X-User-Message 头,前端才肯原样上屏。
    assert resp.headers.get("X-User-Message") == "1"
    detail = resp.json()["detail"]
    assert isinstance(detail, str) and detail
    # 不许把异常原文/字段名漏给用户(user_error 的契约)。
    assert "ValueError" not in detail and "not-a-date" not in detail


def test_activity_wellformed_time_param_still_200(client):
    """反向对照:合法时间参数照常 200。

    没有这一条,上面那条 400 用例可以被一个「所有带 since 的请求都 400」的实现骗过。
    """
    a = _auth(client, 31)
    uid_a = _me(client, a)
    resp = client.get(
        f"/api/admin/users/{uid_a}/activity",
        params={"since": "2026-08-04T00:00:00+08:00",
                "until": "2026-08-05T00:00:00+08:00"},
        headers=a,
    )
    assert resp.status_code == 200


# --- GET /admin/users/{user_id}/notebooks/{notebook_id}/sources ------------


def test_notebook_sources_forbidden_for_other_regular_user(client):
    a = _auth(client, 7)
    b = _auth(client, 8)
    uid_b = _me(client, b)
    nb_id = _create_notebook(client, b, "NB-b")
    resp = client.get(f"/api/admin/users/{uid_b}/notebooks/{nb_id}/sources", headers=a)
    assert resp.status_code == 403


def test_notebook_sources_allowed_for_self(client):
    a = _auth(client, 9)
    uid_a = _me(client, a)
    nb_id = _create_notebook(client, a, "NB-a")
    resp = client.get(f"/api/admin/users/{uid_a}/notebooks/{nb_id}/sources", headers=a)
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"items": [], "total_count": 0, "offset": 0, "limit": 50}


def test_notebook_sources_carry_the_composed_display_title(client):
    """左栏来源清单与中栏活动条目命名的是**同一批来源**,必须给出同一个名字。

    这条端点走的是 list_sources_page → SourceSummary,与活动流那条
    (list_user_activity → 路由层合成) 是两条独立的取数路径。两条各自都「对」
    但拼起来给出两个名字,正是 source_display_title 那条红线要防的:同一篇论文
    在左栏叫 1706.03762.pdf、在中栏叫 Attention Is All You Need。
    """
    a = _auth(client, 11)
    uid_a = _me(client, a)
    nb_id = _create_notebook(client, a, "NB-names")
    with _repo()._write() as db:
        _insert_source(db, "src-paper", nb_id, "2026-08-01T10:00:00",
                       title="uploaded-name.pdf", file_name="1706.03762.pdf")
        _insert_paper_meta(db, "src-paper", nb_id, "Attention Is All You Need")

    resp = client.get(f"/api/admin/users/{uid_a}/notebooks/{nb_id}/sources", headers=a)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["display_title"] == "Attention Is All You Need"
    # 原始列仍照常返回(既有来源页签按它渲染),display_title 是**新增**而不是改写。
    assert items[0]["title"] == "uploaded-name.pdf"


def test_notebook_sources_carry_the_same_raw_timestamp_as_the_activity_feed(client):
    """左栏(来源清单)与中栏(活动流)必须给出**同一个可解析的时间戳**(F5)。

    两个入口点开的是同一份来源,右栏只有一份详情。左栏此前只拿得到
    ``created_label`` —— 那是**服务端**按自己的日历日预先格式化好的「2026年8月4日」,
    而中栏走浏览器本地渲染。服务端 UTC+8、浏览器 UTC 时,同一份来源在左栏是「8月5日」、
    在中栏是「8月4日 17:00」,违反设计稿 §3.1「时间一律显示浏览器本地时区」。

    修法是给 SourceSummary 补原始 ``created_at``,让两条路吃同一个值;``created_label``
    保留只为不动既有来源页签的渲染。
    """
    a = _auth(client, 32)
    uid_a = _me(client, a)
    nb_id = _create_notebook(client, a, "NB-time")
    created = "2026-08-04T06:30:00+08:00"
    with _repo()._write() as db:
        _insert_source(db, "src-time", nb_id, created)

    listed = client.get(
        f"/api/admin/users/{uid_a}/notebooks/{nb_id}/sources", headers=a
    ).json()["items"]
    streamed = client.get(f"/api/admin/users/{uid_a}/activity", headers=a).json()["items"]
    assert len(listed) == 1 and len(streamed) == 1

    # 载荷层的判据:两条独立取数路径回传的时间戳逐字相同,且都是原始 ISO
    # (不是任何一侧预先格式化过的日历日文案)。
    assert listed[0]["created_at"] == created
    assert streamed[0]["created_at"] == created
    # created_label 仍在(既有来源页签在用),但它**不是**新路径的时间来源:
    # 它是服务端日历日,与上面那个可解析的时间戳是两种东西。
    assert listed[0]["created_label"] != listed[0]["created_at"]


def test_notebook_sources_allowed_for_admin(client):
    admin = _auth_admin(client)
    a = _auth(client, 10)
    uid_a = _me(client, a)
    nb_id = _create_notebook(client, a, "NB-a2")
    resp = client.get(f"/api/admin/users/{uid_a}/notebooks/{nb_id}/sources", headers=admin)
    assert resp.status_code == 200


def test_notebook_sources_not_owned_by_target_user_404(client):
    """notebook_id belongs to a different user than the {user_id} path
    segment — must 404 (not leak existence via 403), even for an admin
    caller: the notebook genuinely is not this user's."""
    a = _auth(client, 11)
    b = _auth(client, 12)
    uid_a = _me(client, a)
    nb_id_b = _create_notebook(client, b, "NB-b2")
    admin = _auth_admin(client)
    resp = client.get(
        f"/api/admin/users/{uid_a}/notebooks/{nb_id_b}/sources", headers=admin
    )
    assert resp.status_code == 404


def test_notebook_sources_lists_visible_sources(client):
    a = _auth(client, 13)
    uid_a = _me(client, a)
    nb_id = _create_notebook(client, a, "NB-a3")
    with _repo()._write() as db:
        _insert_source(db, "src-visible", nb_id, "2026-08-01T10:00:00", title="Doc A")
        _insert_source(db, "src-memory", nb_id, "2026-08-01T09:00:00",
                        source_type="memory", title="Memory projection")

    resp = client.get(f"/api/admin/users/{uid_a}/notebooks/{nb_id}/sources", headers=a)
    body = resp.json()
    assert body["total_count"] == 1
    assert [item["id"] for item in body["items"]] == ["src-visible"]


# --- GET /admin/users/{user_id}/asks/{job_id} -------------------------------


def test_ask_detail_forbidden_for_other_regular_user(client):
    a = _auth(client, 14)
    b = _auth(client, 15)
    uid_b = _me(client, b)
    resp = client.get(f"/api/admin/users/{uid_b}/asks/whatever", headers=a)
    assert resp.status_code == 403


def test_ask_detail_allowed_for_self_with_answer(client):
    a = _auth(client, 16)
    uid_a = _me(client, a)
    nb_id = _create_notebook(client, a, "NB-ask")
    with _repo()._write() as db:
        _insert_conversation(db, "conv-1", nb_id, uid_a, "2026-08-01T10:00:00")
        _insert_ask_job(
            db, "job-1", nb_id, uid_a, "2026-08-01T09:50:00",
            conversation_id="conv-1", question="what is x?", mode="chunk",
            status="done", asked_at="2026-08-01T09:55:00+08:00", answer_id="ans-1",
        )
        payload = {
            "answer_id": "ans-1",
            "conclusion": "The answer is 42.",
            "answer": "The answer is 42.",
            "asked_at": "2026-08-01T09:55:00+08:00",
            "mode": "chunk",
            # answered_at deliberately omitted — exercises the backfill from
            # answers.created_at (the authoritative source per the red line).
        }
        _insert_answer(db, "ans-1", nb_id, "conv-1", "what is x?", payload,
                        "2026-08-01T10:00:00")

    resp = client.get(f"/api/admin/users/{uid_a}/asks/job-1", headers=a)
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == "job-1"
    assert body["notebook_id"] == nb_id
    assert body["conversation_id"] == "conv-1"
    assert body["question"] == "what is x?"
    assert body["mode"] == "chunk"
    assert body["status"] == "done"
    assert body["asked_at"] == "2026-08-01T09:55:00+08:00"
    assert body["answered_at"] == "2026-08-01T10:00:00"
    assert body["answer"]["conclusion"] == "The answer is 42."
    assert body["error"] == ""
    assert body["trace"] == []


def test_ask_detail_allowed_for_admin_running_job_uses_active_job_asked_at(client):
    admin = _auth_admin(client)
    a = _auth(client, 17)
    uid_a = _me(client, a)
    nb_id = _create_notebook(client, a, "NB-ask2")
    with _repo()._write() as db:
        _insert_conversation(db, "conv-2", nb_id, uid_a, "2026-08-01T09:00:00")
        _insert_ask_job(
            db, "job-2", nb_id, uid_a, "2026-08-01T09:00:00",
            conversation_id="conv-2", question="q2?", status="running",
            asked_at="2026-08-01T09:00:00+08:00",
        )

    resp = client.get(f"/api/admin/users/{uid_a}/asks/job-2", headers=admin)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "running"
    assert body["answer"] is None
    # No saved answer yet — asked_at still surfaces via the conversation's
    # active_job (mirrors get_conversation's own handling), answered_at stays
    # empty (no fabrication).
    assert body["asked_at"] == "2026-08-01T09:00:00+08:00"
    assert body["answered_at"] == ""


def test_ask_detail_job_not_owned_by_target_user_404(client):
    a = _auth(client, 18)
    b = _auth(client, 19)
    uid_a = _me(client, a)
    uid_b = _me(client, b)
    nb_id = _create_notebook(client, b, "NB-ask3")
    with _repo()._write() as db:
        _insert_ask_job(db, "job-3", nb_id, uid_b, "2026-08-01T09:00:00")

    # job-3 belongs to b; a is asking about "himself" (allowed), but the job
    # is not his — must 404, not silently show another user's ask.
    resp = client.get(f"/api/admin/users/{uid_a}/asks/job-3", headers=a)
    assert resp.status_code == 404

    # Sanity: b can see his own job.
    resp_owner = client.get(f"/api/admin/users/{uid_b}/asks/job-3", headers=b)
    assert resp_owner.status_code == 200


def test_ask_detail_unknown_job_404(client):
    a = _auth(client, 20)
    uid_a = _me(client, a)
    resp = client.get(f"/api/admin/users/{uid_a}/asks/does-not-exist", headers=a)
    assert resp.status_code == 404


# --- 部署开关 -------------------------------------------------------------

def test_activity_endpoints_404_when_debug_logs_disabled(tmp_path, monkeypatch):
    """`DEBUG_LOGS_ENABLED=0` 时三个端点一律 404,和「模型调用」视图同一个判据。

    这条守的是设计稿 §5 隐私论证的**前提**。那句论证是「admin 今天已经能读别人的
    llm.jsonl(里面就是完整 prompt 与答案原文),所以活动流没有扩大暴露面」——它只
    在开关打开时成立。开关默认关闭,若这三个端点不设门,就多出一条绕过该开关、把
    任意用户的提问原文与答案正文交给任何 admin 的路径,暴露面比论证的更宽。

    用 admin 身份查**自己**、且三个目标(笔记本、job)都**真实存在**,把「无权」和
    「找不到」两条路一起排除:唯一能让它 404 的只有开关。

    ⚠ 这里刻意不用 `asks/any-job-id` 这种不存在的 id —— 那样即使把门摘掉,端点也会
    因为「job 不存在」照样 404,断言就变成因错误的原因通过(变异验证实测:摘掉 asks
    那道门,用假 id 的版本仍然全绿)。
    """
    client = _make_client(tmp_path, monkeypatch, debug_logs=False)
    admin = _auth_admin(client)
    uid = _me(client, admin)
    nb_id = _create_notebook(client, admin, "NB-gated")
    with _repo()._write() as db:
        _insert_ask_job(db, "job-gated", nb_id, uid, "2026-08-01T10:00:00",
                        question="开关关闭时这句问题也不该读得到")

    assert client.get(f"/api/admin/users/{uid}/activity", headers=admin).status_code == 404
    assert client.get(
        f"/api/admin/users/{uid}/notebooks/{nb_id}/sources", headers=admin
    ).status_code == 404
    # 存在且归属正确的 job:开关是它 404 的唯一理由。
    assert client.get(
        f"/api/admin/users/{uid}/asks/job-gated", headers=admin
    ).status_code == 404

    # 同一部署下既有的 admin 总览**不**受这个开关影响(它不暴露问答正文),
    # 否则这道门就顺手掰坏了一个无关能力。
    assert client.get("/api/admin/users", headers=admin).status_code == 200
