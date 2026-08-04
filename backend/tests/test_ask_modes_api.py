from fastapi.testclient import TestClient

from tests.ask_testkit import seed_ask_evidence


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "")
    monkeypatch.setenv("EMBED_PROVIDER", "")
    from app.core.config import get_settings
    from app.api import ask_routes
    from app.main import create_app
    get_settings.cache_clear()
    ask_routes.repository.cache_clear()
    return TestClient(create_app())


def test_ask_modes_endpoint_lists_user_facing(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    body = client.get("/api/ask-modes").json()
    assert [m["id"] for m in body] == ["chunk", "reasoning", "graph"]
    assert {m["id"]: m["requires_kg"] for m in body} == {
        "chunk": False, "reasoning": True, "graph": True}


def test_unknown_mode_returns_422_not_silent_fast(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    r = client.post(f"/api/notebooks/{nb}/ask", json={"question": "q", "mode": "bogus"})
    assert r.status_code == 422
    assert "bogus" in str(r.json()["detail"])
    rs = client.post(f"/api/notebooks/{nb}/ask/stream", json={"question": "q", "mode": "bogus"})
    assert rs.status_code == 422


def test_ask_on_empty_notebook_is_rejected_409(tmp_path, monkeypatch):
    """PR#334 硬约束权威闸门:无任何可检索证据的空库,/ask 与 /ask/stream 都以 409
    拒绝(带 X-User-Message 用户文案),不产生凭空回答;塞一条证据后放行。"""
    from app.api.ask_routes import repository
    client = _client(tmp_path, monkeypatch)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]

    r = client.post(f"/api/notebooks/{nb}/ask", json={"question": "q", "mode": "chunk"})
    assert r.status_code == 409
    assert r.headers.get("X-User-Message") == "1"
    assert "来源" in r.json()["detail"]

    rs = client.post(f"/api/notebooks/{nb}/ask/stream", json={"question": "q", "mode": "chunk"})
    assert rs.status_code == 409
    assert rs.headers.get("X-User-Message") == "1"

    # 未知模式(422)先于可用性(409)判定,即便空库也应报模式错。
    bad = client.post(f"/api/notebooks/{nb}/ask/stream", json={"question": "q", "mode": "bogus"})
    assert bad.status_code == 422

    seed_ask_evidence(repository(), nb)
    ok = client.post(f"/api/notebooks/{nb}/ask/stream", json={"question": "q", "mode": "chunk"})
    assert ok.status_code == 200


def test_chunk_mode_streams_start_then_final(tmp_path, monkeypatch):
    import json
    client = _client(tmp_path, monkeypatch)
    from app.api.ask_routes import repository
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    seed_ask_evidence(repository(), nb)  # PR#334:空库 ask 会 409,先塞一条证据
    r = client.post(f"/api/notebooks/{nb}/ask/stream",
                    json={"question": "q", "mode": "chunk"})
    assert r.status_code == 200
    events = [json.loads(l) for l in r.text.splitlines() if l.strip()]
    kinds = [e["event"] for e in events]
    # WS2a: 首事件现为 started(带 job_id,供前端「停止」调 cancel 端点),
    # 随后才是 progress/start。
    assert kinds[0] == "started" and events[0]["job_id"]
    assert events[0]["conversation_id"] == events[-1]["response"]["conversation_id"]
    assert kinds[1] == "progress" and events[1]["step"]["step_type"] == "start"
    assert events[1]["step"]["detail"]["mode"] == "chunk"
    assert kinds[-1] == "final"
    assert "reasoning_trace" not in events[-1]["response"] or \
        not events[-1]["response"]["reasoning_trace"]


def test_ask_stream_runs_through_the_runtime_ask_service(tmp_path, monkeypatch):
    """Task 24: 流式端点经 AskExecutionCoordinator 调 runtime-owned AskService
    (不再是 facade 回调)—— stub 掉服务的 ask 即可拦到整条流的 final 响应。"""
    import json
    from app.api.ask_routes import repository
    from app.models.schemas import AskResponse

    client = _client(tmp_path, monkeypatch)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    repo = repository()
    seed_ask_evidence(repo, nb)  # PR#334:空库 ask 会 409,先塞一条证据
    service = repo._runtime.ask_service()
    seen = {}

    def fake_ask(
        notebook_id,
        payload,
        *,
        user_id,
        on_trace=None,
        cancel_event=None,
        job_id=None,
    ):
        seen["user_id"] = user_id
        return AskResponse(conclusion="service-stub", conversation_id=payload.conversation_id or "")

    monkeypatch.setattr(service, "ask", fake_ask, raising=False)
    r = client.post(f"/api/notebooks/{nb}/ask/stream",
                    json={"question": "q", "mode": "chunk"})
    assert r.status_code == 200
    events = [json.loads(l) for l in r.text.splitlines() if l.strip()]
    assert events[-1]["event"] == "final"
    assert events[-1]["response"]["conclusion"] == "service-stub"
    assert seen["user_id"] == repo.current_user().id


def test_ask_rejects_a_base_scope_naming_an_unmounted_library(tmp_path, monkeypatch):
    """Pins the ``_require_ask_available`` -> ``_validate_base_scope(notebook,
    base_scope)`` call site in ``ask_routes.py``. A mutation that passes
    ``base_scope`` straight through (skipping validation) would let this
    request reach 200 -- the submitted id simply matches no mounted library
    and has no effect on retrieval -- instead of the 422 the checkbox
    contract promises for a scope naming something not actually mounted."""
    client = _client(tmp_path, monkeypatch)
    from app.api.ask_routes import repository
    repo = repository()
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    seed_ask_evidence(repo, nb)  # PR#334: ask_available must be True first

    r = client.post(f"/api/notebooks/{nb}/ask", json={
        "question": "q", "mode": "chunk",
        "base_scope": {"mode": "include", "notebook_ids": ["not-mounted"]},
    })
    assert r.status_code == 422
    assert "参考库" in str(r.json()["detail"])

    rs = client.post(f"/api/notebooks/{nb}/ask/stream", json={
        "question": "q", "mode": "chunk",
        "base_scope": {"mode": "include", "notebook_ids": ["not-mounted"]},
    })
    assert rs.status_code == 422


def test_ask_freezes_an_exclude_nothing_base_scope_before_the_empty_scope_gate(
    tmp_path, monkeypatch
):
    """Pins the same call site from the other side: ``_require_non_empty_scope``
    reads a resolved base scope's ``notebook_ids`` as if it were already
    frozen to ``include`` (the selected set). ``_validate_base_scope`` is
    what performs that freeze -- an unfrozen ``exclude`` scope with an empty
    list means "exclude nothing" (every mounted library still participates),
    but read naively it looks like "nothing selected". On a notebook with NO
    local sources whose only evidence is a mounted library, bypassing
    ``_validate_base_scope`` would misread "exclude nothing" as empty and
    wrongly 409 a request the checkbox contract must accept 200.

    codex #431 R4 (reverting R3): a short-lived intermediate version of
    ``_validate_base_scope`` short-circuited ``exclude`` + ``[]`` straight to
    ``None`` instead of freezing it into an explicit ``include`` list. That
    broke report scope persistence across create/confirm/generate (see
    ``test_report_base_scope_freezes_mount_set_at_create_time`` in
    ``test_report_api.py``), so the freeze is back -- the actual production
    crash it was dodging (overflowing the old 1000-item mount cap) is now
    fixed at the cap instead (raised to 10,000, see
    ``test_validate_base_scope_exclude_nothing_survives_beyond_the_1000_mount_cap``
    in ``test_source_scope.py``)."""
    client = _client(tmp_path, monkeypatch)
    from app.api.ask_routes import repository
    repo = repository()

    base = client.post("/api/notebooks", json={"name": "base lib"}).json()["id"]
    repo.mark_notebook_base(base)
    now = "2026-07-20T00:00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,payload,evidence,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"ko-{base}", base, "concept", "approved", "{}", "[]", now, now),
        )

    active = client.post("/api/notebooks", json={"name": "active"}).json()["id"]
    repo.replace_notebook_bases(active, [base], repo.current_user().id)

    r = client.post(f"/api/notebooks/{active}/ask", json={
        "question": "q", "mode": "chunk",
        "base_scope": {"mode": "exclude", "notebook_ids": []},
    })
    assert r.status_code == 200


def test_ask_intent_preview_opens_the_same_base_scope_ceiling_as_submission(
    tmp_path, monkeypatch
):
    """codex #431 P2 (fixed, not yet pinned): ``preview_ask_intent`` used to
    open ``source_scope_context(notebook_id, resolved_source_scope, None)`` --
    a hardcoded ``None`` for the base-library dimension -- while the eventual
    ``/ask``/``/ask/stream`` submission opens it with ``payload.base_scope``.
    A question naming a source that lives only in an UNCHECKED mounted
    library would then get resolved and signed off during understanding
    (the preview still saw every mounted library), only to fail once actually
    submitted with the checkbox scope applied -- a confirmation the user
    could never act on.

    This pins that the preview now resolves and threads the SAME frozen
    ``base_scope`` ceiling ``_require_ask_available`` already validates for
    submission, by stubbing ``preview_reasoning_intent`` with a probe that
    reads ``current_source_scope()`` from inside the ``source_scope_context``
    block. A mutation reverting the call site to a literal ``None`` third
    argument makes this assert against ``scope is None`` fail."""
    from app.models.ask import QueryIntentContract

    client = _client(tmp_path, monkeypatch)
    from app.api.ask_routes import repository
    repo = repository()

    base = client.post("/api/notebooks", json={"name": "base lib"}).json()["id"]
    repo.mark_notebook_base(base)
    now = "2026-07-20T00:00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,payload,evidence,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"ko-{base}", base, "concept", "approved", "{}", "[]", now, now),
        )

    active = client.post("/api/notebooks", json={"name": "active"}).json()["id"]
    repo.replace_notebook_bases(active, [base], repo.current_user().id)

    captured = {}

    def probe(notebook_id, question, history="", cancel_event=None):
        from app.services.source_scope import current_source_scope
        captured["scope"] = current_source_scope()
        return QueryIntentContract(objective=question, resolved_question=question)

    monkeypatch.setattr(repo, "preview_reasoning_intent", probe)

    r = client.post(f"/api/notebooks/{active}/ask/intent", json={
        "question": "q",
        "base_scope": {"mode": "include", "notebook_ids": [base]},
    })
    assert r.status_code == 200
    scope = captured["scope"]
    assert scope is not None, (
        "resolved base_scope must reach source_scope_context during "
        "/ask/intent, not just at submission"
    )
    assert scope.base_notebook_ids == frozenset({base})
    assert scope.base_mode == "include"


def test_default_full_select_via_full_http_entry_is_not_restricted(
    tmp_path, monkeypatch
):
    """浏览器真实默认形态(两维都发 ``{mode:"exclude", ids:[]}``,即「全选」)经完整
    ``/ask`` HTTP 入口后,``narrowed=False`` 必须让限定模式关闭。

    这是本轮修复的核心场景:codex #431 把每个提交的范围冻结成显式 include 快照
    (防止运行期间新增来源/挂载库扩大范围),但那也让 ``mode`` 恒为 ``include``。
    在加 ``narrowed`` 字段之前,``restricted``/``base_restricted`` 只按 mode/id
    的形状判断,于是默认全选也被判成「已收窄」,私有 Memory、多轮会话历史、PPR、
    社区报告和语料画像全部被静默关掉。

    通过桩替换 ``AskService.ask_chunk``(dispatch 目标,而非 ``ask`` 本身 ——
    ``source_scope_context`` 在 ``AskService.ask`` 内部才进入,桩在 ``ask`` 上
    会跳过整个 ContextVar 装配)在真实请求处理期间原地读取 ContextVar 状态。"""
    from app.models.schemas import AskResponse
    from app.services.source_scope import (
        current_source_scope,
        scoped_conversation_history,
        source_scope_restricted,
    )

    client = _client(tmp_path, monkeypatch)
    from app.api.ask_routes import repository
    repo = repository()
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    seed_ask_evidence(repo, nb)
    service = repo._runtime.ask_service()
    captured = {}

    def probe_ask_chunk(notebook_id, payload, *, user_id, job_id="", cancel_event=None):
        captured["restricted"] = source_scope_restricted()
        scope = current_source_scope()
        captured["base_restricted"] = (
            scope.base_restricted if scope is not None else None
        )
        captured["history"] = scoped_conversation_history("prior")
        # ask_current requires a non-empty answer_id to finish the durable
        # job as "done" -- a bare probe stub otherwise trips
        # "synchronous Ask completed without a durable answer".
        return AskResponse(
            conclusion="probe", conversation_id=payload.conversation_id or "",
            answer_id="probe-answer",
        )

    monkeypatch.setattr(service, "ask_chunk", probe_ask_chunk, raising=False)

    r = client.post(f"/api/notebooks/{nb}/ask", json={
        "question": "q", "mode": "chunk",
        "source_scope": {"mode": "exclude", "source_ids": []},
        "base_scope": {"mode": "exclude", "notebook_ids": []},
    })
    assert r.status_code == 200
    assert captured["restricted"] is False, (
        "全选本地来源不得触发 restricted -- PPR/私有 Memory/社区报告/语料画像"
        "不该被默认状态误关"
    )
    assert captured["base_restricted"] is False, "全选参考库同理"
    assert captured["history"] == "prior", (
        "全选不得清空多轮会话历史 -- 只有真收窄参考库才应清空"
    )


def test_default_full_select_still_produces_an_n_of_n_retrieval_scope_receipt(
    tmp_path, monkeypatch
):
    """⚠ 与直觉相反但已核实为**当前设计**:浏览器全选也发一份显式载荷,后端据此
    照常产出回执(``_scope_receipt`` 只在两个维度都完全没提交、即
    ``resolved_source_scope is None and resolved_base_scope is None`` 时才返回
    ``None`` —— 直连 API 省略两个字段的调用方才会撞到这条,浏览器请求恒不会)。

    抑制这个零信息量的 "N/N" 回执是**前端**职责,不是后端的:见
    ``frontend/app/answer-panel.tsx`` 里 ``narrowed = scope.local.selected <
    scope.local.total || includedBases.length < scope.bases.length`` 那段客户端
    判断,以及 ``frontend/app/answer-retrieval-scope.component.test.tsx`` 的
    "两维都是全量时不渲染回执" 用例(该用例直接构造了一个 selected==total 的
    ``retrieval_scope`` 来断言组件不渲染它,证明后端产出、前端抑制正是既有
    契约)。本用例不走 probe stub(那会跳过 ``_save_answer`` 里实际挂载回执的
    那一步),而是让 chunk 模式的真实请求走完全程。"""
    client = _client(tmp_path, monkeypatch)
    from app.api.ask_routes import repository
    repo = repository()
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    seed_ask_evidence(repo, nb)

    r = client.post(f"/api/notebooks/{nb}/ask", json={
        "question": "q", "mode": "chunk",
        "source_scope": {"mode": "exclude", "source_ids": []},
        "base_scope": {"mode": "exclude", "notebook_ids": []},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["retrieval_scope"] == {
        "local": {"selected": 1, "total": 1}, "bases": []
    }
