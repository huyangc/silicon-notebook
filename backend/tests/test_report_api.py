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
    """回归(D12 在报告路径上曾整个消失):空 allow-list 的报告必须 409,不能建行、
    不能排 plan job —— 否则后台会照跑一轮模型调用产出零证据报告。"""
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
    (notebook.base_notebooks 是「挂了什么」,不是「选了什么」。)"""
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
    assert launched[-1][1]["base_scope"] == {
        "mode": "include", "notebook_ids": [base_id]
    }


def test_report_confirm_and_generate_reject_a_scope_emptied_since_create(
    client, monkeypatch
):
    """创建时非空的范围可能在确认/生成前被掏空(参考库被取消挂载)。两处重新冻结
    之后都必须再判一次 D12,而不是把空范围交给规划/生成。

    这也是与 master 的**平价**回归:改动前 409 长在 _validate_source_scope 里,
    这三个入口各调它一次,所以「本地空 + 一个参考库都不剩」在确认/生成时同样 409。
    刻意省略 base_scope(而不是显式勾掉那个库):显式选中的库被取消挂载会先撞
    _validate_base_scope 的 422,永远走不到 D12。"""
    from app.api.deps import repository

    nb_id, base_id, launched = _scoped_client(client, monkeypatch, with_base=True)
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
    understanding.update(resolved_question="q?", ambiguities=[], needs_clarification=False)
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


def test_base_scope_survives_intent_ready_and_reaches_planning(client, monkeypatch):
    """回归(本特性对报告曾完全 no-op):prepare_intent 用模型新产出的 contract
    **整块替换** understanding_json(report_store 写的是 `understanding_json = ?`,
    不是 merge),所以创建时存下的 base_scope 必须由 prepare_intent 自己补回,
    否则确认/生成两阶段读到的恒为 None、参考库照常全量参与。

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
    assert after_create["base_scope"] == {"mode": "include", "notebook_ids": [base_id]}

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
        "mode": "include", "notebook_ids": [base_id]
    }
    assert after_intent["understanding"]["source_scope"] == {
        "mode": "include", "source_ids": []
    }

    launched.clear()
    confirmed = client.post(
        f"/api/notebooks/{nb_id}/reports/{rid}/intent",
        json={"resolved_question": "分析 PLL 环路稳定性", "answers": []},
    )
    assert confirmed.status_code == 200
    assert launched[-1][1]["base_scope"] == {
        "mode": "include", "notebook_ids": [base_id]
    }
    assert launched[-1][1]["intent_contract"]["base_scope"] == {
        "mode": "include", "notebook_ids": [base_id]
    }


def test_report_without_any_scope_never_fabricates_one(client, monkeypatch):
    """R2:省略两个范围字段的报告必须和本特性接入前逐位相同 —— understanding 里
    不得凭空出现 source_scope/base_scope(凭空造出的 source_scope 会在确认时被冻结
    成 include:[全部可见来源],把整轮翻成 restricted 并关掉私有 Memory 与 PPR)。"""
    from app.api.deps import repository

    nb_id, _, launched = _scoped_client(client, monkeypatch, with_base=True)
    rid = client.post(
        f"/api/notebooks/{nb_id}/reports", json={"question": "q?"}
    ).json()["report_id"]
    assert launched[-1][1] == {}, "无范围时 plan job 的调用形状必须原样不变"
    repo = repository()
    assert repo.get_report(nb_id, rid)["understanding"] == {}


def test_library_only_scope_never_becomes_restricted_across_persistence(
    client, monkeypatch
):
    """R1 跨持久化往返:只取消勾选参考库、本地一个都没动的报告,不得在
    intent_ready 落库时被凭空补出一个 source_scope。

    补出来的 `exclude:[]` 会在确认时被 _validate_source_scope 冻结成
    `include:[全部可见来源]`,其 `restricted` 为 True —— 于是私有 Memory、PPR、
    社区报告与语料画像全部被关掉,而用户只是取消勾选了一个参考库,界面上没有任何
    解释。单元层守得住这条,持久化往返曾从旁边绕过去。"""
    import json

    from app.api.deps import repository
    from app.services.report_engine import ReportEngine
    from app.services.source_scope import (
        source_scope_context,
        source_scope_restricted,
    )
    from tests.model_testkit import bind_chat_client

    class _IntentLLM:
        configured = True

        def chat_json(self, *args, **kwargs):
            return json.dumps({
                "normalized_question": "只缩库范围的问题",
                "mandatory_topics": [{
                    "title": "主题", "question": "问题",
                    "retrieval_queries": ["query"],
                }],
                "needs_clarification": False,
                "ambiguities": [],
            })

    nb_id, base_id, _ = _scoped_client(client, monkeypatch, with_base=True)
    # 上传一份来源,让"全部可见来源"非空 —— 否则冻结出的 include 列表恰好也是空的,
    # restricted 仍会翻成 True 但看不出区别,这条用例就白写了。
    upload = client.post(
        f"/api/notebooks/{nb_id}/sources",
        files=[("files", ("a.md", b"# local corpus", "text/markdown"))],
    )
    assert upload.status_code == 200, upload.text
    from app.api.deps import repository as _repo_for_sources

    assert _repo_for_sources().all_visible_source_ids(nb_id), "本地来源必须真的非空"
    rid = client.post(
        f"/api/notebooks/{nb_id}/reports",
        json={
            "question": "只缩库范围",
            # 刻意只给库维度:本地来源一个都没动
            "base_scope": {"mode": "include", "notebook_ids": []},
        },
    ).json()["report_id"]
    repo = repository()
    after_create = repo.get_report(nb_id, rid)["understanding"]
    assert "source_scope" not in after_create

    for workload_id in ("report_outline", "report_section", "report_summary"):
        bind_chat_client(repo, workload_id, _IntentLLM())
    with source_scope_context(
        nb_id, after_create.get("source_scope"), after_create.get("base_scope")
    ):
        assert source_scope_restricted() is False
        ReportEngine.from_repository(repo, repo.settings).prepare_intent(
            nb_id, rid, "只缩库范围"
        )

    after_intent = repo.get_report(nb_id, rid)["understanding"]
    assert after_intent["base_scope"] == {"mode": "include", "notebook_ids": []}
    assert "source_scope" not in after_intent, (
        "凭空补出的本地范围会在确认时冻结成 include:[全部可见来源] 并翻成 restricted"
    )

    confirmed = client.post(
        f"/api/notebooks/{nb_id}/reports/{rid}/intent",
        json={"resolved_question": "只缩库范围", "answers": []},
    )
    assert confirmed.status_code == 200
    final = repo.get_report(nb_id, rid)["understanding"]
    assert "source_scope" not in final
    # 生成阶段按持久化合同重建 ContextVar(report_execution.start_generate 的做法)。
    with source_scope_context(
        nb_id, final.get("source_scope"), final.get("base_scope")
    ):
        assert source_scope_restricted() is False, "R1:库开关必须与 restricted 正交"


def test_report_create_rejects_a_base_only_submission_on_a_library_only_notebook(
    client, monkeypatch
):
    """省略 source_scope、只提交库维度,且笔记本零本地来源。

    「没提交本地范围」曾被读成「本地非空」,于是这一形状被放行:落一行报告 +
    照常排 plan job(那一步就是一次意图模型调用),产出零证据。这条与上面那条
    的差别只有一处 —— 请求里没有 source_scope 键。
    """
    nb_id, _base_id, launched = _scoped_client(client, monkeypatch, with_base=True)
    from app.api.deps import repository

    assert repository().all_visible_source_ids(nb_id) == [], "前提:零本地来源"

    response = client.post(
        f"/api/notebooks/{nb_id}/reports",
        json={
            "question": "q?",
            # 刻意没有 source_scope 键
            "base_scope": {"mode": "include", "notebook_ids": []},
        },
    )
    assert response.status_code == 409
    assert response.headers.get("X-User-Message") == "1"
    assert launched == [], "被拒的请求绝不能排出 plan job(那是一次意图模型调用)"
    assert client.get(f"/api/notebooks/{nb_id}/reports").json() == [], (
        "被拒的请求绝不能留下报告行"
    )


def test_report_create_without_any_scope_is_untouched_by_the_emptiness_check(
    client, monkeypatch
):
    """R2:两维都没提交时按历史行为走 —— 零可见来源的库照样能建报告。

    这里 D12 无从判断证据宇宙(Knowhow 格子、confirmed Memory、参考库 KG 它都
    看不见),按 counts["sources"] 判空会把这些库的无范围调用直接判死。
    """
    nb_id, _base_id, launched = _scoped_client(client, monkeypatch, with_base=True)
    response = client.post(
        f"/api/notebooks/{nb_id}/reports", json={"question": "q?"}
    )
    assert response.status_code == 200
    assert len(launched) == 1
    assert "source_scope" not in launched[0][1]
    assert "base_scope" not in launched[0][1]
