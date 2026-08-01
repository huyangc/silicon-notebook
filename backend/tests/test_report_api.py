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
    assert client.post(f"/api/notebooks/{nb['id']}/reports/{rid}/cancel").status_code == 200
    assert client.delete(f"/api/notebooks/{nb['id']}/reports/{rid}").status_code == 200
    assert client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").status_code == 404


def test_report_create_rejects_blank_question_and_missing_nb(client, monkeypatch):
    import app.api.report_routes as routes_mod
    monkeypatch.setattr(routes_mod, "_launch_plan_job", lambda *a, **k: None)
    monkeypatch.setattr(routes_mod, "_report_llm_ready", lambda repo: True)
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    r = client.post(f"/api/notebooks/{nb['id']}/reports", json={"question": "   "})
    assert r.status_code == 422
    r = client.post("/api/notebooks/nb-none/reports", json={"question": "q"})
    assert r.status_code == 404


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


def test_outline_patch_preserves_intent_catalog_and_bounds_sections(client, monkeypatch):
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
        {"title": f"S{i}", "scope": " s ", "sub_queries": [" q ", "q2", "q3", "q4", "q5"],
         "intent_ids": ["intent-1" if i == 0 else "intent-2", "unknown"]}
        for i in range(repository().settings.report_max_sections + 2)
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


def test_outline_patch_persists_the_user_confirmed_report_frame(client, monkeypatch):
    import app.api.report_routes as R
    from app.api.deps import repository

    monkeypatch.setattr(R, "_launch_plan_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(R, "_report_llm_ready", lambda repo: True)
    nb = client.post("/api/notebooks", json={"name": "t"}).json()
    rid = client.post(
        f"/api/notebooks/{nb['id']}/reports", json={"question": "compare A and B"}
    ).json()["report_id"]
    repository().update_report(
        nb["id"], rid, status="outline_ready", understanding={"confirmed": True},
        outline=[{"title": "A and B", "scope": "s", "sub_queries": ["A", "B"]}],
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
        json={"sections": [{"title": "A and B", "scope": "s",
                            "sub_queries": ["A", "B"]}], "frame": frame},
    )
    assert response.status_code == 200
    detail = client.get(f"/api/notebooks/{nb['id']}/reports/{rid}").json()
    assert detail["understanding"]["report_frame"] == frame
    assert detail["outline"][0]["report_frame"] == frame


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
