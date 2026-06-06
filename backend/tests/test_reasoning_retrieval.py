def test_trace_step_model_shape():
    from app.models.schemas import TraceStep
    t = TraceStep(step_type="plan", summary="规划了 2 个子查询", detail={"n": 2})
    d = t.model_dump()
    assert d["step_type"] == "plan"
    assert d["summary"].startswith("规划")
    assert d["detail"] == {"n": 2}


def test_ask_request_mode_defaults_fast():
    from app.models.schemas import AskRequest
    assert AskRequest(question="x").mode == "fast"
    assert AskRequest(question="x", mode="reasoning").mode == "reasoning"


def test_ask_response_reasoning_trace_defaults_none_and_dumps():
    from app.models.schemas import AskResponse
    r = AskResponse(conclusion="x")
    assert r.reasoning_trace is None
    assert "reasoning_trace" in r.model_dump()


def test_reasoning_settings_knobs():
    from app.core.config import Settings
    s = Settings()
    assert s.reasoning_max_steps == 50
    assert s.reasoning_max_subqueries == 5


def test_plan_prompt_contains_question_and_schema():
    from app.services.prompts import plan_prompt, PLAN_SCHEMA_HINT
    p = plan_prompt("innovus 的 PR 流程", "User: ...\nAssistant: ...")
    assert "innovus 的 PR 流程" in p
    assert "User: ..." in p  # history_block 被插值进 prompt
    assert "sub_queries" in PLAN_SCHEMA_HINT
    assert "prefer" in PLAN_SCHEMA_HINT


def test_reflect_prompt_contains_summary_and_schema():
    from app.services.prompts import reflect_prompt, REFLECT_SCHEMA_HINT
    p = reflect_prompt("问题X", "- [claim] A (id=k1)")
    assert "问题X" in p
    assert "id=k1" in p
    assert "next_action" in REFLECT_SCHEMA_HINT
    for a in ("answer", "expand_graph", "add_subquery", "search_elements"):
        assert a in REFLECT_SCHEMA_HINT
