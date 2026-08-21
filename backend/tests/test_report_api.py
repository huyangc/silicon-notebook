"""Task 7: 深度报告 API 端点(创建/列表/详情/取消/删除/导出)+ 取消注册表。"""
import io
import threading
import zipfile

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    # 隔离 LLM 端点:清空真实 key/model,保证「LLM 未配置 → 409」分支确定性
    # (不受运行环境 OS env 泄漏影响;与 test_report_engine.py 的 repo fixture 同法)。
    for _k in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
               "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL", "REASONING_LLM_MODEL",
               "REWRITE_LLM_API_KEY", "REWRITE_LLM_BASE_URL", "REWRITE_LLM_MODEL"):
        monkeypatch.setenv(_k, "")
    from app.main import app
    return TestClient(app)


def test_report_endpoints_lifecycle(client, monkeypatch):
    # 建 notebook
    nb = client.post("/api/notebooks", json={"name": "t", "purpose": "p",
                                             "primary_domain": "d"}).json()
    # 起报告:LLM 未配置 → 409
    r = client.post(f"/api/notebooks/{nb['id']}/reports", json={"question": "q"})
    assert r.status_code == 409
    # stub 引擎线程:不真跑(单测不起真深挖)
    import app.api.report_routes as routes_mod
    monkeypatch.setattr(routes_mod, "_launch_plan_job", lambda *a, **k: None)
    monkeypatch.setattr(routes_mod, "_report_llm_ready", lambda repo: True)
    r = client.post(f"/api/notebooks/{nb['id']}/reports", json={"question": "为什么?", "depth": 8})
    assert r.status_code == 200
    rid = r.json()["report_id"]
    lst = client.get(f"/api/notebooks/{nb['id']}/reports").json()
    assert lst[0]["id"] == rid and lst[0]["status"] == "pending"
    assert lst[0]["created_at"] and lst[0]["updated_at"]
    assert lst[0]["generation_started_at"] == ""
    detail = client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").json()
    assert detail["question"] == "为什么?" and "content_md" in detail
    assert detail["created_at"] and detail["updated_at"]
    assert "references" in detail and detail["references"] == []
    assert detail["depth"] == 8
    assert "section_status" in detail
    from app.api.deps import repository
    repo = repository()
    repo.update_report(nb["id"], rid, status="outline_ready")
    assert repo.claim_report_generation(nb["id"], rid)
    repo.update_report(nb["id"], rid, status="done", progress="完成")
    completed = client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").json()
    assert completed["status"] == "done"
    assert completed["generation_started_at"]
    assert completed["updated_at"] >= completed["generation_started_at"]
    assert client.get(f"/api/notebooks/{nb['id']}/reports").json()[0][
        "generation_started_at"
    ] == completed["generation_started_at"]
    cancelled = client.post(f"/api/notebooks/{nb['id']}/reports/{rid}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json() == {"status": "done"}
    assert repo.get_report(nb["id"], rid)["status"] == "done"
    assert client.delete(f"/api/notebooks/{nb['id']}/reports/{rid}").status_code == 200
    assert client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").status_code == 404


def test_terminal_understanding_write_keeps_the_generation_start_stamp(client, monkeypatch):
    """The engine's terminal write must not erase the store's own timing stamp.

    `_generation_started_at` is written by `claim_report_generation`, but the
    engine finishes by persisting its in-memory intent contract — which never
    carries that key.  A plain column assignment therefore wiped it on exactly
    the reports whose duration matters, and every finished report showed no
    elapsed time at all.  The pre-existing coverage missed this because it never
    passed `understanding` to `update_report`.
    """
    import app.api.report_routes as routes_mod
    monkeypatch.setattr(routes_mod, "_launch_plan_job", lambda *a, **k: None)
    monkeypatch.setattr(routes_mod, "_report_llm_ready", lambda repo: True)
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    rid = client.post(
        f"/api/notebooks/{nb['id']}/reports", json={"question": "为什么?"}
    ).json()["report_id"]

    from app.api.deps import repository
    repo = repository()
    repo.update_report(nb["id"], rid, status="outline_ready")
    assert repo.claim_report_generation(nb["id"], rid)
    claimed = client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").json()
    started = claimed["generation_started_at"]
    assert started

    # Exactly what the engine does on the terminal path: persist the intent
    # contract plus credibility, with no knowledge of the private timing key.
    repo.update_report(
        nb["id"], rid, status="done", progress="完成",
        understanding={"objective": "q", "credibility": {"anchor_count": 3}},
    )

    done = client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").json()
    assert done["generation_started_at"] == started
    assert done["understanding"]["objective"] == "q"
    # The private key stays private: it is popped into its own field, never
    # surfaced back inside the user-facing contract.
    assert "_generation_started_at" not in done["understanding"]


def test_report_done_compare_and_set_cannot_overwrite_a_cancelled_generation(
    client, monkeypatch
):
    import app.api.report_routes as routes_mod
    monkeypatch.setattr(routes_mod, "_launch_plan_job", lambda *a, **k: None)
    monkeypatch.setattr(routes_mod, "_report_llm_ready", lambda repo: True)
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    rid = client.post(
        f"/api/notebooks/{nb['id']}/reports", json={"question": "为什么?"}
    ).json()["report_id"]

    from app.api.deps import repository
    repo = repository()
    repo.update_report(nb["id"], rid, status="outline_ready")
    assert repo.claim_report_generation(nb["id"], rid)
    assert repo._runtime.report_store.cancel_report(nb["id"], rid)
    assert repo._runtime.report_store.complete_report_generation(
        nb["id"],
        rid,
        sections=[{"markdown": "must not persist"}],
        content_md="must not persist",
        gaps=[],
        references=[],
    ) is False
    detail = repo.get_report(nb["id"], rid)
    assert detail["status"] == "cancelled"
    assert detail["content_md"] == ""


def test_report_cancel_cannot_overwrite_a_completed_generation(client, monkeypatch):
    import app.api.report_routes as routes_mod
    monkeypatch.setattr(routes_mod, "_launch_plan_job", lambda *a, **k: None)
    monkeypatch.setattr(routes_mod, "_report_llm_ready", lambda repo: True)
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    rid = client.post(
        f"/api/notebooks/{nb['id']}/reports", json={"question": "为什么?"}
    ).json()["report_id"]

    from app.api.deps import repository
    repo = repository()
    repo.update_report(nb["id"], rid, status="outline_ready")
    assert repo.claim_report_generation(nb["id"], rid)
    assert repo._runtime.report_store.complete_report_generation(
        nb["id"], rid,
        sections=[{"markdown": "durable"}],
        content_md="durable",
        gaps=[],
        references=[],
    )

    response = client.post(f"/api/notebooks/{nb['id']}/reports/{rid}/cancel")
    assert response.status_code == 200
    assert response.json() == {"status": "done"}
    detail = repo.get_report(nb["id"], rid)
    assert detail["status"] == "done"
    assert detail["content_md"] == "durable"


def test_report_create_rejects_blank_question_and_missing_nb(client, monkeypatch):
    import app.api.report_routes as routes_mod
    monkeypatch.setattr(routes_mod, "_launch_plan_job", lambda *a, **k: None)
    monkeypatch.setattr(routes_mod, "_report_llm_ready", lambda repo: True)
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    r = client.post(f"/api/notebooks/{nb['id']}/reports", json={"question": "   "})
    assert r.status_code == 422
    r = client.post("/api/notebooks/nb-none/reports", json={"question": "q"})
    assert r.status_code == 404


def test_report_create_refuses_an_over_length_question(client, monkeypatch):
    """研究问题必须在**创建**这一刻就有界(codex #525 R1 P2)。

    公开分享页把 `reports.question` **原样**发给匿名访客(截断用户自撰 artifact
    而不披露违反「用户编辑的数据不得静默截断」),而确认门只写 `resolved_question`
    进 understanding、从不回写这一列——所以「原样返回」只有在创建端点拒收超长问题
    时才是有界的。这条与前端 `REPORT_INPUT_LIMITS.questionMaxChars` 是同一条护栏的
    两半。"""
    from app.models.reports import REPORT_QUESTION_MAX_CHARS
    import app.api.report_routes as routes_mod
    monkeypatch.setattr(routes_mod, "_launch_plan_job", lambda *a, **k: None)
    monkeypatch.setattr(routes_mod, "_report_llm_ready", lambda repo: True)
    nb = client.post("/api/notebooks", json={"name": "t"}).json()

    over = "问" * (REPORT_QUESTION_MAX_CHARS + 1)
    r = client.post(f"/api/notebooks/{nb['id']}/reports", json={"question": over})
    assert r.status_code == 422
    # 拒绝，不是裁短了存：库里不能留下一份被悄悄截过的问题。
    assert client.get(f"/api/notebooks/{nb['id']}/reports").json() == []

    # 恰好等于上限仍然收——上限是护栏不是陷阱(空转保护:一个恒 422 的实现过不了)。
    at_cap = "问" * REPORT_QUESTION_MAX_CHARS
    ok = client.post(f"/api/notebooks/{nb['id']}/reports", json={"question": at_cap})
    assert ok.status_code == 200, ok.text


def test_cancel_registry_live_thread_path(client, monkeypatch):
    """取消先持久化终态，再通知当前进程的活动线程。"""
    from app.services.report_engine import (
        register_cancel, cancel_report, unregister_cancel)
    import app.api.report_routes as routes_mod
    monkeypatch.setattr(routes_mod, "_launch_plan_job", lambda *a, **k: None)
    monkeypatch.setattr(routes_mod, "_report_llm_ready", lambda repo: True)
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    rid = client.post(f"/api/notebooks/{nb['id']}/reports",
                      json={"question": "q"}).json()["report_id"]
    ev = register_cancel(rid)
    r = client.post(f"/api/notebooks/{nb['id']}/reports/{rid}/cancel")
    assert r.json()["status"] == "cancelled" and ev.is_set()
    unregister_cancel(rid, ev)
    assert cancel_report(rid) is False           # 注销后不再命中活动线程
    # 线程已结束路径:再 cancel → 直接落库 cancelled
    r = client.post(f"/api/notebooks/{nb['id']}/reports/{rid}/cancel")
    assert r.json()["status"] == "cancelled"
    detail = client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").json()
    assert detail["status"] == "cancelled"


def test_cancel_endpoint_wins_race_with_terminal_report_write(client, monkeypatch):
    import app.api.report_routes as routes_mod
    from app.api.deps import repository

    monkeypatch.setattr(routes_mod, "_launch_plan_job", lambda *a, **k: None)
    monkeypatch.setattr(routes_mod, "_report_llm_ready", lambda repo: True)
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    rid = client.post(
        f"/api/notebooks/{nb['id']}/reports", json={"question": "q"}
    ).json()["report_id"]
    store = repository()._runtime.report_store
    terminal_ready = threading.Event()
    cancel_committed = threading.Event()
    original_update = store.update_report

    def blocked_terminal(*args, **kwargs):
        if kwargs.get("status") == "done":
            terminal_ready.set()
            assert cancel_committed.wait(timeout=5)
        return original_update(*args, **kwargs)

    monkeypatch.setattr(store, "update_report", blocked_terminal)
    worker = threading.Thread(
        target=store.update_report,
        args=(nb["id"], rid),
        kwargs={"status": "done", "content_md": "# too late"},
    )
    worker.start()
    assert terminal_ready.wait(timeout=5)
    response = client.post(f"/api/notebooks/{nb['id']}/reports/{rid}/cancel")
    assert response.status_code == 200
    assert response.json() == {"status": "cancelled"}
    cancel_committed.set()
    worker.join(timeout=5)
    assert not worker.is_alive()

    detail = client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").json()
    assert detail["status"] == "cancelled"
    assert detail["content_md"] == ""


def _mk_report(client, monkeypatch, nb_id, question, *, done=False, content_md=""):
    """建一个报告;done=True 时用 repo.update_report 直接置 status/content_md。"""
    import app.api.report_routes as routes_mod
    monkeypatch.setattr(routes_mod, "_launch_plan_job", lambda *a, **k: None)
    monkeypatch.setattr(routes_mod, "_report_llm_ready", lambda repo: True)
    rid = client.post(f"/api/notebooks/{nb_id}/reports",
                      json={"question": question}).json()["report_id"]
    if done:
        from app.api.deps import repository
        repository().update_report(nb_id, rid, status="done", content_md=content_md)
    return rid


def test_report_export_zip_only_done(client, monkeypatch):
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    done1 = _mk_report(client, monkeypatch, nb["id"], "第一问 为什么?",
                       done=True, content_md="# R1\n正文一")
    done2 = _mk_report(client, monkeypatch, nb["id"], "第二问",
                       done=True, content_md="# R2\n正文二")
    pending = _mk_report(client, monkeypatch, nb["id"], "未完成问")   # 仍 pending
    resp = client.post(f"/api/notebooks/{nb['id']}/reports/export",
                       json={"report_ids": [done1, done2, pending, "rep-不存在"]})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/zip")
    assert "attachment" in resp.headers.get("content-disposition", "")
    assert "reports.zip" in resp.headers.get("content-disposition", "")
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = zf.namelist()
    assert len(names) == 2                       # 只 2 份 done,pending/不存在被跳过
    assert all(n.endswith(".md") for n in names)
    blob = "\n".join(zf.read(n).decode("utf-8") for n in names)
    assert "# R1" in blob and "# R2" in blob


def test_report_export_empty_ids_rejected(client, monkeypatch):
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    resp = client.post(f"/api/notebooks/{nb['id']}/reports/export",
                       json={"report_ids": []})
    assert resp.status_code == 422


def test_report_export_skips_other_notebook_report(client, monkeypatch):
    """跨 notebook 隔离:另一 notebook 的 done 报告 id 传入本 notebook 导出 → 被跳过。
    仅传该外部 id 时无可导出内容 → 422。"""
    nb_a = client.post("/api/notebooks", json={"name": "a"}).json()
    nb_b = client.post("/api/notebooks", json={"name": "b"}).json()
    foreign = _mk_report(client, monkeypatch, nb_b["id"], "别人的报告",
                         done=True, content_md="# FOREIGN")
    resp = client.post(f"/api/notebooks/{nb_a['id']}/reports/export",
                       json={"report_ids": [foreign]})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Task 6(两阶段): POST /reports 起规划 job → outline_ready → PATCH 大纲 → generate
# ---------------------------------------------------------------------------

def test_two_phase_report_lifecycle(client, monkeypatch):
    import app.api.report_routes as R
    from app.api.deps import repository
    from tests.model_testkit import bind_chat_client

    class ConfiguredReportClient:
        configured = True

    report_repo = repository()
    bind_chat_client(report_repo, "report_outline", ConfiguredReportClient())
    bind_chat_client(report_repo, "report_section", ConfiguredReportClient())
    launched = {}
    monkeypatch.setattr(R, "_launch_plan_job", lambda repo,nb,rid,q,h,ag: launched.setdefault("plan", (rid, ag)))
    monkeypatch.setattr(R, "_launch_generate_job", lambda repo,nb,rid,q,d: launched.setdefault("gen", rid))
    nb = client.post("/api/notebooks", json={"name":"t","purpose":"p","primary_domain":"d"}).json()
    r = client.post(f"/api/notebooks/{nb['id']}/reports", json={"question":"why?"})
    rid = r.json()["report_id"]; assert launched["plan"][0]==rid and launched["plan"][1] is False
    # 模拟 planning 完成:写 outline_ready + outline
    report_repo.update_report(nb["id"], rid, status="outline_ready",
                              outline=[{"title":"A","scope":"s","sub_queries":["q"]}])
    d = client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").json()
    assert d["status"]=="outline_ready" and d["outline"][0]["title"]=="A"
    # 编辑大纲
    assert client.patch(f"/api/notebooks/{nb['id']}/reports/{rid}/outline",
                        json={"sections":[{"title":"A2","scope":"s","sub_queries":["q2"]}]}).status_code==200
    assert client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").json()["outline"][0]["title"]=="A2"
    # 触发生成
    assert client.post(f"/api/notebooks/{nb['id']}/reports/{rid}/generate", json={}).status_code==200
    assert launched["gen"]==rid
    duplicate_generate = client.post(
        f"/api/notebooks/{nb['id']}/reports/{rid}/generate", json={}
    )
    assert duplicate_generate.status_code == 409


def test_intent_confirmation_requires_answers_then_resumes_planning(client, monkeypatch):
    import app.api.report_routes as R
    from app.api.deps import repository

    monkeypatch.setattr(R, "_report_llm_ready", lambda repo: True)
    launched = []
    monkeypatch.setattr(
        R,
        "_launch_plan_job",
        lambda *args, **kwargs: launched.append((args, kwargs)),
    )
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    rid = client.post(
        f"/api/notebooks/{nb['id']}/reports",
        json={"question": "分析一下这个问题"},
    ).json()["report_id"]
    understanding = {
        "objective": "分析一下这个问题",
        "resolved_question": "分析这个问题",
        "mandatory_topics": [{
            "id": "intent-1", "title": "待明确", "question": "分析什么？",
            "retrieval_queries": ["分析"],
        }],
        "ambiguities": [{
            "id": "ambiguity-input",
            "question": "具体研究对象是什么？",
            "required": True,
            "options": [],
        }],
        "needs_clarification": True,
        "confirmed": False,
    }
    repository().update_report(
        nb["id"], rid, status="intent_ready", understanding=understanding
    )

    missing = client.post(
        f"/api/notebooks/{nb['id']}/reports/{rid}/intent",
        json={"resolved_question": "分析 PLL 稳定性", "answers": []},
    )
    assert missing.status_code == 422
    assert missing.json()["detail"] == "请先回答所有必填澄清问题"

    confirmed = client.post(
        f"/api/notebooks/{nb['id']}/reports/{rid}/intent",
        json={
            "resolved_question": "分析 PLL 环路稳定性的机理与设计约束",
            "answers": [{"id": "ambiguity-input", "answer": "对象是电荷泵 PLL"}],
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.json() == {"status": "planning"}
    detail = client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").json()
    assert detail["status"] == "planning"
    seed = detail["understanding"]["confirmed_input"]
    assert seed["resolved_question"].startswith("分析 PLL")
    assert seed["answers"][0]["answer"] == "对象是电荷泵 PLL"
    assert launched[-1][1]["intent_contract"]["confirmed"] is True

    launched_after_claim = len(launched)
    duplicate = client.post(
        f"/api/notebooks/{nb['id']}/reports/{rid}/intent",
        json={
            "resolved_question": "重复确认不应启动第二个任务",
            "answers": [{"id": "ambiguity-input", "answer": "另一个对象"}],
        },
    )
    assert duplicate.status_code == 409
    assert len(launched) == launched_after_claim


def test_outline_patch_preserves_intent_catalog_at_section_limit(client, monkeypatch):
    import app.api.report_routes as R
    from app.api.deps import repository

    monkeypatch.setattr(R, "_launch_plan_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(R, "_report_llm_ready", lambda repo: True)
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    rid = client.post(
        f"/api/notebooks/{nb['id']}/reports", json={"question": "compare A and B"}
    ).json()["report_id"]
    catalog = [
        {"id": "intent-1", "title": "A", "question": "explain A",
         "retrieval_queries": ["A"]},
        {"id": "intent-2", "title": "B", "question": "explain B",
         "retrieval_queries": ["B"]},
    ]
    contract = {"objective": "compare A and B", "mandatory_topics": catalog,
                "comparison_axes": ["latency"]}
    repository().update_report(
        nb["id"], rid, status="outline_ready",
        outline=[{"title": "A", "scope": "s", "sub_queries": ["A"],
                  "intent_ids": ["intent-1"], "intent_catalog": catalog,
                  "intent_contract": contract}],
    )
    submitted = [
        {"title": f"S{i}", "scope": " s ", "sub_queries": [" q ", "q2", "q3", "q4"],
         "intent_ids": ["intent-1" if i == 0 else "intent-2", "unknown"]}
        for i in range(repository().settings.report_max_sections)
    ]
    response = client.patch(
        f"/api/notebooks/{nb['id']}/reports/{rid}/outline",
        json={"sections": submitted},
    )
    assert response.status_code == 200
    outline = client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").json()["outline"]
    assert len(outline) == repository().settings.report_max_sections
    assert outline[0]["sub_queries"] == ["q", "q2", "q3", "q4"]
    assert outline[0]["intent_ids"] == ["intent-1"]
    assert outline[0]["intent_questions"] == ["explain A"]
    assert all(section["intent_catalog"] == catalog for section in outline)
    assert all(section["intent_contract"] == contract for section in outline)


def test_outline_patch_rejects_excess_sections(client, monkeypatch):
    import app.api.report_routes as R
    from app.api.deps import repository

    monkeypatch.setattr(R, "_launch_plan_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(R, "_report_llm_ready", lambda repo: True)
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    rid = client.post(
        f"/api/notebooks/{nb['id']}/reports", json={"question": "q"}
    ).json()["report_id"]
    repository().update_report(
        nb["id"], rid, status="outline_ready",
        outline=[{"title": "A", "scope": "s", "sub_queries": ["q"]}],
    )
    cap = repository().settings.report_max_sections
    response = client.patch(
        f"/api/notebooks/{nb['id']}/reports/{rid}/outline",
        json={"sections": [
            {"title": f"S{i}", "scope": "s", "sub_queries": [f"q{i}"]}
            for i in range(cap + 1)
        ]},
    )
    assert response.status_code == 422
    assert str(cap) in response.json()["detail"]


def test_outline_patch_rejects_excess_retrieval_directions(client, monkeypatch):
    import app.api.report_routes as R
    from app.api.deps import repository

    monkeypatch.setattr(R, "_launch_plan_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(R, "_report_llm_ready", lambda repo: True)
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    rid = client.post(
        f"/api/notebooks/{nb['id']}/reports", json={"question": "q"}
    ).json()["report_id"]
    repository().update_report(
        nb["id"], rid, status="outline_ready",
        outline=[{"title": "A", "scope": "s", "sub_queries": ["q"]}],
    )
    cap = repository().settings.report_max_subqueries_per_section
    response = client.patch(
        f"/api/notebooks/{nb['id']}/reports/{rid}/outline",
        json={"sections": [{
            "title": "A",
            "scope": "s",
            "sub_queries": [f"q{i}" for i in range(cap + 1)],
        }]},
    )
    assert response.status_code == 422
    assert str(cap) in response.json()["detail"]


def test_outline_patch_persists_the_user_confirmed_report_frame(client, monkeypatch):
    import app.api.report_routes as R
    from app.api.deps import repository

    monkeypatch.setattr(R, "_launch_plan_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(R, "_report_llm_ready", lambda repo: True)
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    rid = client.post(
        f"/api/notebooks/{nb['id']}/reports", json={"question": "compare A and B"}
    ).json()["report_id"]
    old_frame = {
        "subject_kind": "model",
        "facets": [{"id": "mixer", "name": "Sequence mixer",
                    "values": ["Attention", "SSM"], "exclusive": False}],
        "axes": [],
        "instance_policy": "old",
    }
    old_contract = {"confirmed": True, "report_frame": old_frame}
    repository().update_report(
        nb["id"], rid, status="outline_ready",
        understanding={"confirmed": True, "report_frame": old_frame},
        outline=[
            {"title": "A", "scope": "s", "sub_queries": ["A"],
             "intent_contract": old_contract},
            {"title": "B", "scope": "s", "sub_queries": ["B"],
             "intent_contract": old_contract},
        ],
    )
    frame = {
        "subject_kind": "model",
        "facets": [{"id": "mixer", "name": "Sequence mixer",
                    "values": ["Attention", "SSM"], "exclusive": True}],
        "axes": [{"id": "latency", "name": "Latency",
                  "condition_fields": ["context length"]}],
        "instance_policy": "An instance can combine mechanisms across facets.",
    }
    response = client.patch(
        f"/api/notebooks/{nb['id']}/reports/{rid}/outline",
        json={"sections": [
            {"title": "A", "scope": "s", "sub_queries": ["A"]},
            {"title": "B", "scope": "s", "sub_queries": ["B"]},
        ], "frame": frame},
    )
    assert response.status_code == 200
    detail = client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").json()
    assert detail["understanding"]["report_frame"] == frame
    assert all(section["report_frame"] == frame for section in detail["outline"])
    assert all(
        section["intent_contract"]["report_frame"] == frame
        for section in detail["outline"]
    )

    from app.services.report_engine import ReportEngine
    engine = ReportEngine.from_repository(repository(), repository().settings)
    claims = [
        {"entities": ["Mamba"], "frame_assignments": {"mixer": value}}
        for value in ("Attention", "SSM")
    ]
    content, _, _ = engine._assemble(
        nb["id"], rid, "compare A and B", detail["outline"],
        [
            {"title": "A", "scope": "s", "markdown": "## A\ntext",
             "grounded": True, "claims": [claims[0]], "id_map": {}},
            {"title": "B", "scope": "s", "markdown": "## B\ntext",
             "grounded": True, "claims": [claims[1]], "id_map": {}},
        ],
    )
    assert "分析框架冲突" in content
    generated = client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").json()
    assert generated["understanding"]["report_frame"] == frame


def test_outline_patch_clears_the_user_confirmed_report_frame_end_to_end(
    client, monkeypatch
):
    import app.api.report_routes as R
    from app.api.deps import repository
    from app.services.report_engine import ReportEngine

    monkeypatch.setattr(R, "_launch_plan_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(R, "_report_llm_ready", lambda repo: True)
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    rid = client.post(
        f"/api/notebooks/{nb['id']}/reports", json={"question": "compare A and B"}
    ).json()["report_id"]
    old_frame = {
        "subject_kind": "model",
        "facets": [{
            "id": "mixer", "name": "Sequence mixer",
            "values": ["Attention", "SSM"], "exclusive": True,
        }],
        "axes": [],
        "instance_policy": "old",
    }
    old_contract = {"confirmed": True, "report_frame": old_frame}
    old_sections = [
        {
            "title": title, "scope": "s", "sub_queries": [title],
            "report_frame": old_frame, "intent_contract": old_contract,
        }
        for title in ("A", "B")
    ]
    repository().update_report(
        nb["id"], rid, status="outline_ready",
        understanding={"confirmed": True, "report_frame": old_frame},
        outline=old_sections,
    )

    # The browser submits its edited section objects, so each row can still
    # contain the old section-level copy while the explicit frame is cleared.
    response = client.patch(
        f"/api/notebooks/{nb['id']}/reports/{rid}/outline",
        json={"sections": old_sections, "frame": {}},
    )
    assert response.status_code == 200
    detail = client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").json()
    assert "report_frame" not in detail["understanding"]
    assert all("report_frame" not in section for section in detail["outline"])
    assert all(
        "report_frame" not in section.get("intent_contract", {})
        for section in detail["outline"]
    )

    claims = [
        {"entities": ["Mamba"], "frame_assignments": {"mixer": value}}
        for value in ("Attention", "SSM")
    ]
    engine = ReportEngine.from_repository(repository(), repository().settings)
    content, _, _ = engine._assemble(
        nb["id"], rid, "compare A and B", detail["outline"],
        [
            {
                "title": title, "scope": "s", "markdown": f"## {title}\ntext",
                "grounded": True, "claims": [claim], "id_map": {},
            }
            for title, claim in zip(("A", "B"), claims)
        ],
    )
    assert "分析框架冲突" not in content
    generated = client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").json()
    assert "report_frame" not in generated["understanding"]


def test_legacy_section_frame_wins_over_stale_embedded_intent_frame(client, monkeypatch):
    """Old outline_ready rows can contain two disagreeing frame copies.

    The section copy is what the user confirmed in the outline editor.  The
    planner-era intent copy remains a compatibility mirror only, including for
    both final understanding persistence and exclusive-facet conflict audit.
    """
    import app.api.report_routes as R
    from app.api.deps import repository
    from app.services.report_engine import ReportEngine

    monkeypatch.setattr(R, "_launch_plan_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(R, "_report_llm_ready", lambda repo: True)
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    rid = client.post(
        f"/api/notebooks/{nb['id']}/reports", json={"question": "compare A and B"}
    ).json()["report_id"]
    stale_intent_frame = {
        "subject_kind": "model",
        "facets": [{
            "id": "mixer", "name": "Sequence mixer",
            "values": ["Attention", "SSM"], "exclusive": False,
        }],
        "axes": [],
        "instance_policy": "planner-era mirror",
    }
    confirmed_section_frame = {
        "subject_kind": "model",
        "facets": [{
            "id": "mixer", "name": "Sequence mixer",
            "values": ["Attention", "SSM"], "exclusive": True,
        }],
        "axes": [],
        "instance_policy": "user-confirmed outline",
    }
    repository().update_report(
        nb["id"], rid, status="outline_ready",
        understanding={"confirmed": True, "report_frame": stale_intent_frame},
        outline=[{
            "title": title, "scope": "s", "sub_queries": [title],
            "report_frame": confirmed_section_frame,
            "intent_contract": {
                "confirmed": True, "report_frame": stale_intent_frame,
            },
        } for title in ("A", "B")],
    )
    outline = client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").json()["outline"]
    engine = ReportEngine.from_repository(repository(), repository().settings)
    content, _, _ = engine._assemble(
        nb["id"], rid, "compare A and B", outline,
        [{
            "title": title, "scope": "s", "markdown": f"## {title}\ntext",
            "grounded": True,
            "claims": [{
                "entities": ["Mamba"], "frame_assignments": {"mixer": value},
            }],
            "id_map": {},
        } for title, value in (("A", "Attention"), ("B", "SSM"))],
    )

    assert "分析框架冲突" in content
    generated = client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").json()
    assert generated["understanding"]["report_frame"] == confirmed_section_frame


def test_outline_patch_rejects_a_malformed_user_frame(client, monkeypatch):
    import app.api.report_routes as R
    from app.api.deps import repository

    monkeypatch.setattr(R, "_launch_plan_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(R, "_report_llm_ready", lambda repo: True)
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    rid = client.post(
        f"/api/notebooks/{nb['id']}/reports", json={"question": "compare A and B"}
    ).json()["report_id"]
    repository().update_report(
        nb["id"], rid, status="outline_ready",
        outline=[{"title": "A", "scope": "s", "sub_queries": ["A"]}],
    )
    response = client.patch(
        f"/api/notebooks/{nb['id']}/reports/{rid}/outline",
        json={"sections": [{"title": "A", "scope": "s", "sub_queries": ["A"]}],
              "frame": {"facets": "not-a-list"}},
    )
    assert response.status_code == 422
    assert response.headers["X-User-Message"] == "1"


def test_outline_patch_rejects_dropping_a_mandatory_intent(client, monkeypatch):
    import app.api.report_routes as R
    from app.api.deps import repository

    monkeypatch.setattr(R, "_launch_plan_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(R, "_report_llm_ready", lambda repo: True)
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    rid = client.post(
        f"/api/notebooks/{nb['id']}/reports", json={"question": "compare A and B"}
    ).json()["report_id"]
    catalog = [
        {"id": "intent-1", "title": "A", "question": "explain A", "retrieval_queries": ["A"]},
        {"id": "intent-2", "title": "B", "question": "explain B", "retrieval_queries": ["B"]},
    ]
    repository().update_report(
        nb["id"], rid, status="outline_ready",
        outline=[{"title": "A and B", "scope": "s", "sub_queries": ["A", "B"],
                  "intent_ids": ["intent-1", "intent-2"], "intent_catalog": catalog,
                  "intent_contract": {"objective": "compare A and B",
                                      "mandatory_topics": catalog}}],
    )

    response = client.patch(
        f"/api/notebooks/{nb['id']}/reports/{rid}/outline",
        json={"sections": [{"title": "Only A", "scope": "s", "sub_queries": ["A"],
                            "intent_ids": ["intent-1"]}]},
    )

    assert response.status_code == 422
    assert response.headers["X-User-Message"] == "1"
    assert response.json()["detail"] == "大纲必须保留每个必答主题，请恢复被删除的主题后再试"

def test_generate_rejects_when_not_outline_ready(client, monkeypatch):
    import app.api.report_routes as R
    monkeypatch.setattr(R,"_report_llm_ready",lambda repo:True)
    monkeypatch.setattr(R,"_launch_plan_job",lambda *a,**k:None)
    nb=client.post("/api/notebooks",json={"name":"t","purpose":"p","primary_domain":"d"}).json()
    rid=client.post(f"/api/notebooks/{nb['id']}/reports",json={"question":"q"}).json()["report_id"]
    # 仍 planning(无 outline)→ generate 应 409
    assert client.post(f"/api/notebooks/{nb['id']}/reports/{rid}/generate",json={}).status_code==409


def test_generate_retries_failed_report_from_confirmed_outline(client, monkeypatch):
    import app.api.report_routes as R
    from app.api.deps import repository

    monkeypatch.setattr(R, "_report_llm_ready", lambda repo: True)
    monkeypatch.setattr(R, "_launch_plan_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(repository()._runtime.models, "configured", lambda _workload: True)
    launched = []
    monkeypatch.setattr(
        R, "_launch_generate_job",
        lambda *args, **kwargs: launched.append((args, kwargs)),
    )
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    rid = client.post(
        f"/api/notebooks/{nb['id']}/reports", json={"question": "q"}
    ).json()["report_id"]
    repository().update_report(
        nb["id"], rid, status="failed", error="pool timeout",
        outline=[{"title": "A", "scope": "s", "sub_queries": ["q"]}],
        content_md="# stale", sections=[{"title": "A", "markdown": "stale"}],
    )

    response = client.post(
        f"/api/notebooks/{nb['id']}/reports/{rid}/generate", json={}
    )

    assert response.status_code == 200
    assert len(launched) == 1
    detail = repository().get_report(nb["id"], rid)
    assert detail["status"] == "generating"
    assert detail["outline"][0]["title"] == "A"
    assert detail["content_md"] == "" and detail["sections"] == []


def test_generate_does_not_retry_failed_report_without_outline(client, monkeypatch):
    import app.api.report_routes as R
    from app.api.deps import repository

    monkeypatch.setattr(R, "_report_llm_ready", lambda repo: True)
    monkeypatch.setattr(R, "_launch_plan_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(repository()._runtime.models, "configured", lambda _workload: True)
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    rid = client.post(
        f"/api/notebooks/{nb['id']}/reports", json={"question": "q"}
    ).json()["report_id"]
    repository().update_report(nb["id"], rid, status="failed", error="planning")

    response = client.post(
        f"/api/notebooks/{nb['id']}/reports/{rid}/generate", json={}
    )

    assert response.status_code == 409
    assert response.headers["X-User-Message"] == "1"


# ---------------------------------------------------------------------------
# 检索范围(来源 + 参考库两个维度)在报告三个入口上的权威预检与持久化往返
# ---------------------------------------------------------------------------


def _scoped_client(client, monkeypatch, *, with_base=False):
    """建一个 notebook(可选挂一个参考库),并 stub 掉 LLM 就绪与 plan job。

    返回 (notebook_id, base_notebook_id|None, launched) —— launched 记录每次
    _launch_plan_job 的 (args, kwargs),用于断言范围真的传给了后台任务。
    """
    import app.api.report_routes as R
    from app.api.deps import repository

    monkeypatch.setattr(R, "_report_llm_ready", lambda repo: True)
    launched: list = []
    monkeypatch.setattr(
        R, "_launch_plan_job", lambda *args, **kwargs: launched.append((args, kwargs))
    )
    monkeypatch.setattr(R, "_launch_generate_job", lambda *args, **kwargs: None)
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    base_id = None
    if with_base:
        base = client.post("/api/notebooks", json={"name": "base"}).json()
        repository().mark_notebook_base(base["id"])
        put = client.put(
            f"/api/notebooks/{nb['id']}/bases",
            json={"base_notebook_ids": [base["id"]]},
        )
        assert put.status_code == 200
        base_id = base["id"]
    return nb["id"], base_id, launched


def test_report_create_rejects_an_empty_local_scope_with_no_mounted_library(
    client, monkeypatch
):
    """回归(空范围预检从 _validate_source_scope 搬到 _require_non_empty_scope 之后
    在报告路径上必须仍然生效):空 allow-list 的报告必须 409,不能建行、不能排
    plan job —— 否则后台会照跑一轮模型调用产出零证据报告。"""
    nb_id, _, launched = _scoped_client(client, monkeypatch)
    response = client.post(
        f"/api/notebooks/{nb_id}/reports",
        json={"question": "q?", "source_scope": {"mode": "include", "source_ids": []}},
    )
    assert response.status_code == 409
    assert response.headers.get("X-User-Message") == "1"
    assert response.json()["detail"] == "当前检索范围为空，请至少选择一个来源或挂载参考库"
    assert launched == [], "被拒的请求绝不能排出 plan job"
    assert client.get(f"/api/notebooks/{nb_id}/reports").json() == []


def test_report_create_rejects_when_every_mounted_library_is_unchecked(
    client, monkeypatch
):
    """挂着参考库不等于范围非空:本地为空 + 库全部取消勾选,仍必须 409。
    (notebook.base_notebooks 是「挂了什么」,不是「这次勾了什么」。)"""
    nb_id, base_id, launched = _scoped_client(client, monkeypatch, with_base=True)
    response = client.post(
        f"/api/notebooks/{nb_id}/reports",
        json={
            "question": "q?",
            "source_scope": {"mode": "include", "source_ids": []},
            "base_scope": {"mode": "include", "notebook_ids": []},
        },
    )
    assert response.status_code == 409
    assert launched == []
    # 同一空本地范围,但保留那个参考库 → 放行(证明 409 判的是两个维度的合取)。
    ok = client.post(
        f"/api/notebooks/{nb_id}/reports",
        json={
            "question": "q?",
            "source_scope": {"mode": "include", "source_ids": []},
            "base_scope": {"mode": "include", "notebook_ids": [base_id]},
        },
    )
    assert ok.status_code == 200
    # 唯一挂载的库被显式选中 → 覆盖了整个宇宙 → 不算收窄。
    assert launched[-1][1]["base_scope"].model_dump() == {
        "mode": "include", "notebook_ids": [base_id], "narrowed": False
    }


def test_report_create_rejects_a_base_only_submission_on_a_library_only_notebook(
    client, monkeypatch
):
    """只提交库维度、省略 source_scope:省略的那一维必须按**真实证据宇宙**判空,
    不能当成「本地非空」放行。"""
    nb_id, _base_id, launched = _scoped_client(client, monkeypatch, with_base=True)
    response = client.post(
        f"/api/notebooks/{nb_id}/reports",
        json={"question": "q?", "base_scope": {"mode": "include", "notebook_ids": []}},
    )
    assert response.status_code == 409
    assert launched == []


def test_report_create_without_any_scope_is_untouched_by_the_emptiness_check(
    client, monkeypatch
):
    """两维都没提交的请求不是一次「选择」,这道闸完全不参与 —— 报告可以合法建在
    一个 Ask 框会拒绝的笔记本上,收紧那一点是另一个行为变更。"""
    nb_id, _base_id, launched = _scoped_client(client, monkeypatch)
    created = client.post(f"/api/notebooks/{nb_id}/reports", json={"question": "q?"})
    assert created.status_code == 200
    assert launched and "source_scope" not in launched[-1][1]
    assert "base_scope" not in launched[-1][1]
    from app.api.deps import repository

    stored = repository().get_report(nb_id, created.json()["report_id"])
    assert (stored.get("understanding") or {}).get("base_scope") is None
    assert (stored.get("understanding") or {}).get("source_scope") is None


def test_report_confirm_and_generate_reject_a_scope_emptied_since_create(
    client, monkeypatch
):
    """创建时非空的范围可能在确认/生成前被掏空(参考库被取消挂载)。两处重新冻结
    之后都必须再判一次空范围,而不是把空范围交给规划/生成。

    刻意省略 base_scope(而不是显式勾掉那个库):显式选中的库被取消挂载会先撞
    _validate_base_scope 的 422,永远走不到空范围那一判。"""
    from app.api.deps import repository

    nb_id, _base_id, launched = _scoped_client(client, monkeypatch, with_base=True)
    rid = client.post(
        f"/api/notebooks/{nb_id}/reports",
        json={
            "question": "q?",
            "source_scope": {"mode": "include", "source_ids": []},
        },
    ).json()["report_id"]
    # 取消挂载 → 本地空范围此刻已无任何参考库兜底
    assert client.put(
        f"/api/notebooks/{nb_id}/bases", json={"base_notebook_ids": []}
    ).status_code == 200
    repo = repository()
    understanding = repo.get_report(nb_id, rid)["understanding"]
    understanding.update(
        resolved_question="q?", ambiguities=[], needs_clarification=False
    )
    repo.update_report(nb_id, rid, status="intent_ready", understanding=understanding)

    launched.clear()
    confirmed = client.post(
        f"/api/notebooks/{nb_id}/reports/{rid}/intent",
        json={"resolved_question": "q?", "answers": []},
    )
    assert confirmed.status_code == 409
    assert confirmed.json()["detail"] == "当前检索范围为空，请至少选择一个来源或挂载参考库"
    assert launched == []
    assert repo.get_report(nb_id, rid)["status"] == "intent_ready", "不得认领 planning"

    repo.update_report(nb_id, rid, status="outline_ready",
                       outline=[{"title": "A", "scope": "s", "sub_queries": ["q"]}])
    # generate 走的是 models.configured("report_section") 而不是 _report_llm_ready,
    # 单独绑一个已配置的桩,免得 409 是「LLM 未配置」那条、白测。
    from tests.model_testkit import bind_chat_client

    class _Configured:
        configured = True

    bind_chat_client(repo, "report_section", _Configured())
    generated = client.post(f"/api/notebooks/{nb_id}/reports/{rid}/generate", json={})
    assert generated.status_code == 409
    assert generated.json()["detail"] == "当前检索范围为空，请至少选择一个来源或挂载参考库"
    assert repo.get_report(nb_id, rid)["status"] == "outline_ready", "不得认领 generating"


def test_report_intent_and_generate_accept_large_server_frozen_scope(
    client, monkeypatch
):
    """A report persists the server-expanded source ceiling between phases.

    More than 10,000 ids would be rejected as a public request, but this value
    is the server's own trusted snapshot.  Both later HTTP gates must restore
    it with ``ResolvedSourceScope`` before re-freezing; switching either gate
    back to the request model would turn this regression into a 409 again.
    """
    import app.api.report_routes as routes_mod
    from app.api.deps import repository
    from tests.model_testkit import bind_chat_client

    monkeypatch.setattr(routes_mod, "_report_llm_ready", lambda repo: True)
    plan_launches = []
    generate_launches = []
    nb_id, _, _ = _scoped_client(client, monkeypatch)
    monkeypatch.setattr(
        routes_mod,
        "_launch_plan_job",
        lambda *args, **kwargs: plan_launches.append((args, kwargs)),
    )
    monkeypatch.setattr(
        routes_mod,
        "_launch_generate_job",
        lambda *args, **kwargs: generate_launches.append((args, kwargs)),
    )

    created = client.post(
        f"/api/notebooks/{nb_id}/reports", json={"question": "q?"}
    )
    assert created.status_code == 200
    rid = created.json()["report_id"]
    repo = repository()
    source_ids = [f"s{index}" for index in range(10_001)]
    understanding = {
        "source_scope": {
            "mode": "include",
            "source_ids": source_ids,
            "narrowed": False,
        },
        "resolved_question": "q?",
        "ambiguities": [],
        "needs_clarification": False,
    }
    repo.update_report(
        nb_id, rid, status="intent_ready", understanding=understanding
    )

    # Isolate the persisted-model boundary under test from repository source
    # ownership reads. The route must successfully parse the trusted snapshot
    # before this helper receives it.
    monkeypatch.setattr(
        routes_mod, "_validate_source_scope", lambda _repo, _nb, scope: scope
    )
    confirmed = client.post(
        f"/api/notebooks/{nb_id}/reports/{rid}/intent",
        json={"resolved_question": "q?", "answers": []},
    )
    assert confirmed.status_code == 200
    confirmed_scope = plan_launches[-1][1]["source_scope"]
    assert len(confirmed_scope.source_ids) == 10_001

    repo.update_report(
        nb_id,
        rid,
        status="outline_ready",
        outline=[{"title": "A", "scope": "s", "sub_queries": ["q"]}],
    )

    class _Configured:
        configured = True

    bind_chat_client(repo, "report_section", _Configured())
    generated = client.post(
        f"/api/notebooks/{nb_id}/reports/{rid}/generate", json={}
    )
    assert generated.status_code == 200
    generated_scope = generate_launches[-1][1]["source_scope"]
    assert len(generated_scope.source_ids) == 10_001


def test_report_base_scope_freezes_mount_set_at_create_time(client, monkeypatch):
    """R6:报告的范围是**创建时定格、跨阶段持久化**的(create → confirm → generate)。
    `exclude:[]`——浏览器「全选参考库」的紧凑表示——必须在 create 时就把当时挂载的
    库集合冻结成显式 include 快照并原样持久化,不能短路成 None 留到确认/生成阶段
    再按 notebook **此刻**的挂载集重新展开;否则报告创建之后新挂载的参考库会静默
    参与这份报告。"""
    from app.api.deps import repository

    nb_id, base_id, launched = _scoped_client(client, monkeypatch, with_base=True)
    created = client.post(
        f"/api/notebooks/{nb_id}/reports",
        json={
            "question": "q?",
            "source_scope": {"mode": "include", "source_ids": []},
            "base_scope": {"mode": "exclude", "notebook_ids": []},
        },
    )
    assert created.status_code == 200
    rid = created.json()["report_id"]
    repo = repository()
    after_create = repo.get_report(nb_id, rid)["understanding"]
    assert after_create["base_scope"] == {
        "mode": "include", "notebook_ids": [base_id], "narrowed": False
    }, "create 时必须把 exclude:[] 就地展开成当时挂载集的显式 include 快照,不能落 None"

    # 创建之后再挂一个新库 —— 已冻结的范围绝不能因此扩大。
    new_base = client.post("/api/notebooks", json={"name": "base2"}).json()
    repository().mark_notebook_base(new_base["id"])
    assert client.put(
        f"/api/notebooks/{nb_id}/bases",
        json={"base_notebook_ids": [base_id, new_base["id"]]},
    ).status_code == 200

    understanding = repo.get_report(nb_id, rid)["understanding"]
    understanding.update(
        resolved_question="q?", ambiguities=[], needs_clarification=False
    )
    repo.update_report(nb_id, rid, status="intent_ready", understanding=understanding)

    launched.clear()
    confirmed = client.post(
        f"/api/notebooks/{nb_id}/reports/{rid}/intent",
        json={"resolved_question": "q?", "answers": []},
    )
    assert confirmed.status_code == 200
    # confirm 用 notebook **当前**的挂载集(现在是 2 个库)重新冻结,于是那份 1 个库
    # 的快照如今确实覆盖不到整个宇宙 —— narrowed 翻真。这是正确读法:本 run 必须
    # 按 base_restricted 处理,新挂上的库不得混进联邦检索,这正是冻结的意义。
    assert launched[-1][1]["base_scope"].model_dump() == {
        "mode": "include", "notebook_ids": [base_id], "narrowed": True
    }, "confirm 之后新挂载的库不得混进已冻结的范围"
    after_intent = repo.get_report(nb_id, rid)["understanding"]
    assert after_intent["base_scope"] == {
        "mode": "include", "notebook_ids": [base_id], "narrowed": True
    }


def test_base_scope_survives_intent_ready_and_reaches_planning(client, monkeypatch):
    """回归(本特性对报告曾完全 no-op 的形状):prepare_intent 用模型新产出的
    contract **整块替换** understanding_json(report_store 写的是
    `understanding_json = ?`,不是 merge),所以创建时存下的 base_scope 必须由
    prepare_intent 自己补回,否则确认/生成两阶段读到的恒为 None、参考库照常全量
    参与。

    这里跑完整链路:create(存范围) → 计划任务在 source_scope_context 里调
    prepare_intent(与 report_execution.start_plan 逐字同构) → confirm(重新冻结
    并把范围交给下一段 plan job)。"""
    import json

    from app.api.deps import repository
    from app.services.report_engine import ReportEngine
    from app.services.source_scope import source_scope_context
    from tests.model_testkit import bind_chat_client

    class _IntentLLM:
        configured = True

        def chat_json(self, *args, **kwargs):
            return json.dumps({
                "normalized_question": "PLL 环路稳定性的机理是什么？",
                "intent_type": "explain",
                "mandatory_topics": [{
                    "title": "环路稳定性",
                    "question": "PLL 环路稳定性的机理是什么？",
                    "retrieval_queries": ["PLL loop stability"],
                }],
                "needs_clarification": False,
                "ambiguities": [],
            })

    nb_id, base_id, launched = _scoped_client(client, monkeypatch, with_base=True)
    created = client.post(
        f"/api/notebooks/{nb_id}/reports",
        json={
            "question": "分析 PLL 稳定性",
            "source_scope": {"mode": "include", "source_ids": []},
            "base_scope": {"mode": "include", "notebook_ids": [base_id]},
        },
    )
    assert created.status_code == 200
    rid = created.json()["report_id"]
    repo = repository()
    after_create = repo.get_report(nb_id, rid)["understanding"]
    assert after_create["base_scope"] == {
        "mode": "include", "notebook_ids": [base_id], "narrowed": False
    }

    # 计划任务:report_execution.start_plan 只经 ContextVar 传范围(它并不给
    # engine.run 传 source_scope/base_scope),所以 prepare_intent 的兜底读取
    # 就是这条链路上唯一的补回点。
    for workload_id in ("report_outline", "report_section", "report_summary"):
        bind_chat_client(repo, workload_id, _IntentLLM())
    with source_scope_context(
        nb_id, after_create.get("source_scope"), after_create.get("base_scope")
    ):
        ReportEngine.from_repository(repo, repo.settings).prepare_intent(
            nb_id, rid, "分析 PLL 稳定性"
        )

    after_intent = repo.get_report(nb_id, rid)
    assert after_intent["status"] == "intent_ready"
    assert after_intent["understanding"]["base_scope"] == {
        "mode": "include", "notebook_ids": [base_id], "narrowed": False
    }
    assert after_intent["understanding"]["source_scope"] == {
        "mode": "include", "source_ids": [], "narrowed": False,
    }

    launched.clear()
    confirmed = client.post(
        f"/api/notebooks/{nb_id}/reports/{rid}/intent",
        json={"resolved_question": "分析 PLL 环路稳定性", "answers": []},
    )
    assert confirmed.status_code == 200
    assert launched[-1][1]["base_scope"].model_dump() == {
        "mode": "include", "notebook_ids": [base_id], "narrowed": False
    }
    assert launched[-1][1]["intent_contract"]["base_scope"] == {
        "mode": "include", "notebook_ids": [base_id], "narrowed": False
    }


def test_library_only_report_scope_never_becomes_locally_restricted(
    client, monkeypatch
):
    """R1 跨持久化:只取消参考库勾选的报告,在**每一个**阶段读回来之后都必须让
    `source_scope_restricted()` 保持 False —— 否则该报告的整库画像、PPR、私有
    Memory 与社区报告会被静默关掉,而用户只是少借了一个参考库。

    关键在于两处都不得凭空造出一份本地范围:create 不写 source_scope,
    prepare_intent 的 `current_source_scope_payload()` 兜底也必须返回 None。"""
    from app.api.deps import repository
    from app.services.source_scope import (
        base_scope_restricted,
        source_scope_restricted,
        source_scope_context,
    )

    nb_id, base_id, _launched = _scoped_client(client, monkeypatch, with_base=True)
    new_base = client.post("/api/notebooks", json={"name": "base2"}).json()
    repository().mark_notebook_base(new_base["id"])
    assert client.put(
        f"/api/notebooks/{nb_id}/bases",
        json={"base_notebook_ids": [base_id, new_base["id"]]},
    ).status_code == 200

    created = client.post(
        f"/api/notebooks/{nb_id}/reports",
        json={
            "question": "q?",
            # 只提交库维度,并且真的少勾了一个。
            "base_scope": {"mode": "include", "notebook_ids": [base_id]},
        },
    )
    assert created.status_code == 200
    understanding = repository().get_report(
        nb_id, created.json()["report_id"]
    )["understanding"]
    assert understanding.get("source_scope") is None, "不得替用户伪造一份本地范围"
    assert understanding["base_scope"]["narrowed"] is True

    with source_scope_context(
        nb_id, understanding.get("source_scope"), understanding.get("base_scope")
    ):
        assert base_scope_restricted() is True
        assert source_scope_restricted() is False
    import app.api.report_routes as R
    from app.api.deps import repository

    monkeypatch.setattr(R, "_report_llm_ready", lambda repo: True)


def test_public_report_share_link_is_anonymous_and_bounded(client, monkeypatch):
    """A share link is readable without a session and discloses an allowlist.

    The anonymous route binds no request user, so it must never reach an
    owner-scoped repository method: `current_user` falls back to the seeded
    admin when the ContextVar is unset.
    """
    import app.api.report_routes as routes_mod
    monkeypatch.setattr(routes_mod, "_launch_plan_job", lambda *a, **k: None)
    monkeypatch.setattr(routes_mod, "_report_llm_ready", lambda repo: True)
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    rid = client.post(
        f"/api/notebooks/{nb['id']}/reports", json={"question": "为什么?"}
    ).json()["report_id"]

    # Unfinished reports cannot be shared: the link would show a half-written body.
    assert client.post(
        f"/api/notebooks/{nb['id']}/reports/{rid}/share"
    ).status_code == 409

    from app.api.deps import repository
    repository().update_report(
        nb["id"], rid, status="done", content_md="# 标题\n\n正文 [k1]",
        references=[{
            "key": "k1", "label": "文件.pdf", "source_title": "论文标题",
            "source_file_name": "文件.pdf", "location_label": "第 3 页",
            "snippet": "被引用的原文片段",
            # None of these may cross to an anonymous reader.
            "source_id": "src-secret", "element_id": "el-secret",
            "object_id": "ko-secret", "family_key": "source:src-secret",
            # T6（深度报告引用卡附图）：images/asset_id 与 source_id/element_id
            # 同类内部 handle，绝不跨出公开投影（见 report_public_view.py 的
            # 白名单 docstring）。
            "images": [{
                "element_id": "el-secret-fig", "asset_id": "asset-secret-1",
                "caption": "机密图注",
            }],
        }],
        understanding={"objective": "内部意图", "source_scope": {"source_ids": ["src-secret"]}},
    )
    token = client.post(
        f"/api/notebooks/{nb['id']}/reports/{rid}/share"
    ).json()["share_token"]
    assert token
    # A capability token, not a row id: `new_id` truncates a UUID to 32 bits,
    # which is enumerable on an unauthenticated, unthrottled endpoint.
    assert token.startswith("rshr-")
    assert len(token) - len("rshr-") >= 40
    # Idempotent: a link already handed out must not start 404ing.
    assert client.post(
        f"/api/notebooks/{nb['id']}/reports/{rid}/share"
    ).json()["share_token"] == token

    # The detail projection says *whether* it is shared and never carries the
    # credential: that endpoint only requires read permission, so a reader would
    # otherwise be able to hand out anonymous access without write access.
    detail = client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").json()
    assert detail["shared"] is True
    assert "share_token" not in detail
    # Sanity: the authenticated (owner-visible) projection is unfiltered — it
    # really does carry images/asset_id. This is what makes the public
    # allowlist below a meaningful assertion rather than a vacuous one.
    assert detail["references"][0]["images"][0]["asset_id"] == "asset-secret-1"
    assert token not in client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").text
    # The token is readable only through the write-guarded endpoint.
    assert client.get(
        f"/api/notebooks/{nb['id']}/reports/{rid}/share"
    ).json()["share_token"] == token

    public = client.get(f"/api/public/reports/{token}")
    assert public.status_code == 200
    body = public.json()
    assert body["content_md"] == "# 标题\n\n正文 [k1]"
    assert body["references"][0]["title"] == "论文标题"
    assert body["references"][0]["snippet"] == "被引用的原文片段"
    assert body["references"][0]["location"] == "第 3 页"
    assert "images" not in body["references"][0]
    # The allowlist holds: no internal handles, no intent contract.
    serialized = public.text
    for secret in (
        "src-secret", "el-secret", "ko-secret", "内部意图",
        "el-secret-fig", "asset-secret-1", "机密图注",
    ):
        assert secret not in serialized
    assert "understanding" not in body and "notebook_id" not in body

    # Revoking makes it indistinguishable from a token that never existed.
    assert client.delete(
        f"/api/notebooks/{nb['id']}/reports/{rid}/share"
    ).status_code == 204
    assert client.get(f"/api/public/reports/{token}").status_code == 404
    assert client.get("/api/public/reports/rshr-never-issued").status_code == 404
    # Unshared again: the detail flag flips back and the read-back 404s.
    assert client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").json()["shared"] is False
    assert client.get(
        f"/api/notebooks/{nb['id']}/reports/{rid}/share"
    ).status_code == 404


def test_public_report_route_is_not_behind_the_router_level_session_guard():
    """The public read must not sit under the blanket `get_current_user` router.

    `main.py` mounts the main API router with a router-level
    `Depends(get_current_user)` ("zero per-route omissions"). A public endpoint
    declared on that router answers 401 to the very visitors it exists for —
    and the auth-optional test fixture hides it, because there an absent token
    silently resolves to the seeded admin. So assert the wiring itself.
    """
    from app.api.deps import get_current_user
    from app.main import create_app

    app = create_app()
    public = [
        route for route in app.routes
        if getattr(route, "path", "") == "/api/public/reports/{token}"
    ]
    assert public, "public report route is not registered"
    for route in public:
        dependencies = [
            dependency.call
            for dependency in getattr(route, "dependant", None).dependencies
        ] if getattr(route, "dependant", None) else []
        assert get_current_user not in dependencies, (
            "public report route inherited the session guard"
        )

    # The authenticated report routes must still carry a guard, so this test
    # cannot pass by removing authentication everywhere.
    guarded = [
        route for route in app.routes
        if getattr(route, "path", "") == "/api/notebooks/{notebook_id}/reports/{report_id}/share"
    ]
    assert guarded, "share route is not registered"


def test_capability_tokens_are_not_row_ids():
    """A share token is an authorization grant, so it needs credential entropy.

    `event_logging.new_id` truncates a UUID to eight hex characters — fine for a
    primary key behind a UNIQUE constraint, but 32 bits is enumerable on an
    unauthenticated endpoint with no rate limiting.
    """
    from app.core.capability_tokens import new_capability_token
    from app.core.event_logging import new_id

    assert len(new_id("rshr")) - len("rshr-") == 8      # 何以不能用它

    tokens = {new_capability_token("rshr") for _ in range(500)}
    assert len(tokens) == 500                            # 无碰撞
    for token in tokens:
        assert token.startswith("rshr-")
        body = token[len("rshr-"):]
        assert len(body) >= 40                           # ≥ 240 bits base64url
        assert set(body) <= set(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        )


def test_unshared_reports_store_null_not_an_empty_timestamp(client, monkeypatch):
    """`shared_at` must be NULL when unshared, never `''`.

    PostgreSQL types this column as a nullable `timestamptz`, and the forward
    shadow's timestamp converter only maps *registered* empty sentinels to NULL.
    An unregistered `''` would call `datetime.fromisoformat("")` for every
    unshared report — nearly every row — and abort the migration.
    """
    import sqlite3

    import app.api.report_routes as routes_mod
    monkeypatch.setattr(routes_mod, "_launch_plan_job", lambda *a, **k: None)
    monkeypatch.setattr(routes_mod, "_report_llm_ready", lambda repo: True)
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    rid = client.post(
        f"/api/notebooks/{nb['id']}/reports", json={"question": "q"}
    ).json()["report_id"]

    from app.api.deps import repository
    repo = repository()
    path = repo._runtime.settings.sqlite_path
    connection = sqlite3.connect(path)
    try:
        column = {
            row[1]: row for row in connection.execute("PRAGMA table_info(reports)")
        }["shared_at"]
        assert column[3] == 0, "shared_at must stay nullable"   # notnull
        assert column[4] is None, "shared_at must not default to a sentinel"

        def stored():
            return connection.execute(
                "SELECT shared_at FROM reports WHERE id=?", (rid,)
            ).fetchone()[0]

        assert stored() is None                                  # 新建即 NULL
        repo.update_report(nb["id"], rid, status="done")
        client.post(f"/api/notebooks/{nb['id']}/reports/{rid}/share")
        assert stored() is not None                              # 分享后有时刻
        client.delete(f"/api/notebooks/{nb['id']}/reports/{rid}/share")
        assert stored() is None                                  # 撤销回到 NULL
    finally:
        connection.close()


def test_concurrent_share_calls_converge_on_one_token(client, monkeypatch):
    """Two concurrent shares must hand back the same link.

    NOTE: on SQLite this passes even with a read-then-write implementation,
    because `database.write()` serialises writers. The race this guards is a
    PostgreSQL READ COMMITTED one, and the mutation that actually proves the
    conditional update lives in the G3 conformance lane
    (`postgres/test_core_store_conformance.py`). Keep this one as the
    contract-level statement that the API is idempotent on both backends.
    """
    import threading

    import app.api.report_routes as routes_mod
    monkeypatch.setattr(routes_mod, "_launch_plan_job", lambda *a, **k: None)
    monkeypatch.setattr(routes_mod, "_report_llm_ready", lambda repo: True)
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    rid = client.post(
        f"/api/notebooks/{nb['id']}/reports", json={"question": "q"}
    ).json()["report_id"]
    from app.api.deps import repository
    repo = repository()
    repo.update_report(nb["id"], rid, status="done")

    start = threading.Barrier(4)
    results: list[str] = []
    lock = threading.Lock()

    def issue():
        start.wait(timeout=5)
        token = repo.share_report(nb["id"], rid)
        with lock:
            results.append(token)

    threads = [threading.Thread(target=issue) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(results) == 4
    assert len(set(results)) == 1, results
    # And the surviving link is the one actually persisted.
    assert repo.report_share_token(nb["id"], rid) == results[0]
    assert client.get(f"/api/public/reports/{results[0]}").status_code == 200


def test_auto_generate_flag_reaches_create_and_survives_manual_confirmation(
    client, monkeypatch,
):
    """auto_generate 是持久 flag:创建时传入,确认后从 understanding 读回。

    歧义停过一次的报告,人工确认意图后大纲阶段仍须自动直通(第 6 个位置参数就是
    引擎的 ``auto_generate``)。
    """
    import app.api.report_routes as R
    from app.api.deps import repository

    monkeypatch.setattr(R, "_report_llm_ready", lambda repo: True)
    launched = []
    monkeypatch.setattr(
        R, "_launch_plan_job",
        lambda *args, **kwargs: launched.append((args, kwargs)),
    )
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    rid = client.post(
        f"/api/notebooks/{nb['id']}/reports",
        json={"question": "分析一下这个问题", "auto_generate": True},
    ).json()["report_id"]
    assert launched[-1][0][5] is True, "创建时未把 auto_generate 传给规划 job"

    repository().update_report(
        nb["id"], rid, status="intent_ready",
        understanding={
            "objective": "分析一下这个问题",
            "resolved_question": "分析这个问题",
            "auto_generate_requested": True,
            "ambiguities": [{
                "id": "ambiguity-input",
                "question": "具体研究对象是什么？",
                "required": True,
                "options": [],
            }],
            "needs_clarification": True,
            "confirmed": False,
        },
    )
    confirmed = client.post(
        f"/api/notebooks/{nb['id']}/reports/{rid}/intent",
        json={
            "resolved_question": "分析 PLL 环路稳定性",
            "answers": [{"id": "ambiguity-input", "answer": "电荷泵 PLL"}],
        },
    )
    assert confirmed.status_code == 200
    assert launched[-1][0][5] is True, "人工确认后 flag 丢失,大纲不会自动直通"
    assert launched[-1][1]["intent_contract"]["auto_generate_requested"] is True


def test_manual_confirmation_without_the_flag_stays_at_the_outline_gate(
    client, monkeypatch,
):
    """默认(高级)模式逐位不变:确认意图后 auto_generate 仍为 False。"""
    import app.api.report_routes as R
    from app.api.deps import repository

    monkeypatch.setattr(R, "_report_llm_ready", lambda repo: True)
    launched = []
    monkeypatch.setattr(
        R, "_launch_plan_job",
        lambda *args, **kwargs: launched.append((args, kwargs)),
    )
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    rid = client.post(
        f"/api/notebooks/{nb['id']}/reports", json={"question": "分析 PLL 稳定性"},
    ).json()["report_id"]
    assert launched[-1][0][5] is False

    repository().update_report(
        nb["id"], rid, status="intent_ready",
        understanding={
            "objective": "分析 PLL 稳定性",
            "resolved_question": "分析 PLL 环路稳定性",
            "auto_generate_requested": False,
            "ambiguities": [],
            "needs_clarification": False,
            "confirmed": False,
        },
    )
    confirmed = client.post(
        f"/api/notebooks/{nb['id']}/reports/{rid}/intent",
        json={"resolved_question": "分析 PLL 环路稳定性", "answers": []},
    )
    assert confirmed.status_code == 200
    assert launched[-1][0][5] is False


def test_report_intent_confirmation_rejects_a_blank_resolved_question(
    client, monkeypatch,
):
    """空研究问题走共享冻结逻辑的 422,并且绝不认领 planning。"""
    import app.api.report_routes as R
    from app.api.deps import repository

    monkeypatch.setattr(R, "_report_llm_ready", lambda repo: True)
    launched = []
    monkeypatch.setattr(
        R, "_launch_plan_job",
        lambda *args, **kwargs: launched.append(args),
    )
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    rid = client.post(
        f"/api/notebooks/{nb['id']}/reports", json={"question": "分析 PLL 稳定性"},
    ).json()["report_id"]
    repository().update_report(
        nb["id"], rid, status="intent_ready",
        understanding={
            "objective": "分析 PLL 稳定性",
            "resolved_question": "分析 PLL 环路稳定性",
            "ambiguities": [],
            "needs_clarification": False,
            "confirmed": False,
        },
    )
    launched.clear()

    blank = client.post(
        f"/api/notebooks/{nb['id']}/reports/{rid}/intent",
        json={"resolved_question": "   ", "answers": []},
    )
    assert blank.status_code == 422
    assert blank.json()["detail"] == "确认后的研究问题不能为空"
    assert blank.headers.get("X-User-Message") == "1"
    assert launched == []
    assert repository().get_report(nb["id"], rid)["status"] == "intent_ready"
