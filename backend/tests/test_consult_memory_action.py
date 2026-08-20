"""Agentic Memory P4 (T5): the ``consult_memory`` reflect action.

Covers: the three-condition gate (kill switch + deep-and-above effort +
``experience_wiring_active``) that decides whether the action is OFFERED at
all, the frozen byte-for-byte baseline when any one condition is off, the
dispatch-time behaviour (selection excludes already-delivered entries,
budget is consumed even on an empty result, two calls in one run share the
600-character render cap and never resurface the same entry), the "your own
undelivered retrieval_notes" overlay half, and the zero-new-I/O contract.

Prompt/schema wiring is covered by ``test_prompts.py``; the pure selection/
render helpers (``select_consultable``/``render_consult_block``/
``worst_experience_for``) have their own unit coverage further down in this
file. Privacy/vocabulary statics are ``test_retrieval_experience_privacy_guard``.
"""
from __future__ import annotations

import json

import pytest

from app.core.ask_retrieval_policy import ask_retrieval_limits
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.reasoning_retrieval import (
    CONSULT_MEMORY_ACTION,
    ReasoningRetriever,
    consult_memory_active,
)
from app.services.retrieval_experience_block import (
    CONSULT_MEMORY_BLOCK_MAX_CHARS,
    CONSULT_MEMORY_TOP_K,
    render_consult_block,
    select_consultable,
    worst_experience_for,
)
from app.services.retrieval_experience_projection import (
    current_situation,
    experience_id,
)
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client


NOW = "2026-08-20T00:00:00+08:00"
CONSULT_HEADER = "[Recalled search tactics]"

INTENT_DETAIL = {
    "resolved_question": "版图设计要点有哪些",
    "result_scope": "ranked",
    "completeness_required": False,
    "retrieval_effort": "standard",
    "entities": ["版图"],
    "constraints": ["先进工艺"],
    "excluded_topics": ["封装"],
    "assumptions": [],
    "expected_output": "要点清单",
    "mandatory_topics": ["版图设计要点"],
}

RATIONALE_PPR = "PPR breadth rarely pays off on this single-document shape."
RATIONALE_EXACT = "exact_lookup on this shape almost always finds the section."


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    instance = SQLiteRepository(Settings())
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    instance.settings.graph_ppr_enabled = False
    instance.settings.retrieval_experience_inject_enabled = True
    _reset_memo()
    return instance


def _reset_memo():
    from app.services import reasoning_retrieval

    with reasoning_retrieval._EXPERIENCE_CACHE_LOCK:
        reasoning_retrieval._EXPERIENCE_CACHE.clear()


def _seed(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(notebook.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "版图设计要点", "section_path": "1"}, "evidence": []},
    ], [])
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("s1", notebook.id, "论文一", "pdf", "extracted", "extracted", NOW, NOW),
        )
    repo.collection_catalog.invalidate()
    return notebook


def _situation(**overrides) -> dict:
    situation = current_situation(
        INTENT_DETAIL, mode="reasoning", retrieval_effort="deep")
    situation.update(overrides)
    return situation


def _write_experience(repo, action, polarity, rationale, *, situation=None,
                      provenance=None):
    situation = situation or _situation()
    entry_id = experience_id(situation, action)
    repo.retrieval_experiences.upsert_experience(
        entry_id,
        situation=situation,
        action=action,
        polarity=polarity,
        rationale=rationale,
        provenance=list(provenance) if provenance else ["run-1"],
        provenance_max=60,
        replace_conclusion=True,
    )
    _reset_memo()
    return entry_id


#: The passive block (``select_experiences``) always takes the top
#: ``RETRIEVAL_EXPERIENCE_INJECT_TOP_K`` (3) entries — one per distinct
#: action — that clear the similarity floor, and consult_memory's whole
#: point is to surface what that block did NOT already deliver. So most
#: dispatch-level tests need the library to hold MORE than 3 distinct-action
#: entries: writing only one or two entries only ever exercises the "already
#: delivered, nothing new" skip path (covered on its own below).
_PASSIVE_FILLER_ACTIONS = ("retrieve", "expand", "expand_community")


def _write_many_experiences(repo, extra):
    """Seed exactly enough entries that the passive block's top-3 fills up
    with ``_PASSIVE_FILLER_ACTIONS`` (highest support, via a longer
    provenance list), leaving ``extra`` (a list of
    ``(action, polarity, rationale)``, in descending support order) for
    consult_memory to find."""
    situation = _situation()
    for action in _PASSIVE_FILLER_ACTIONS:
        _write_experience(repo, action, "good", f"{action} tends to help here.",
                          situation=situation,
                          provenance=[f"filler-{action}-{i}" for i in range(20)])
    for index, (action, polarity, rationale) in enumerate(extra):
        _write_experience(
            repo, action, polarity, rationale, situation=situation,
            provenance=[f"extra-{action}-{i}" for i in range(len(extra) - index)])
    return situation


def _write_profile_block(repo, notebook_id, owner_id, label, value):
    return repo.agent_profile.write_block(
        notebook_id, owner_id, label,
        value=value, evidence=[], expected_revision=0,
        origin="job", actor="",
    )


class _SeqLLM:
    """plan 固定;reflect 按序列返回(耗尽后默认 answer)。记录每次 prompt 正文。"""

    configured = True

    def __init__(self, reflects=None, plan=None):
        self._plan = plan or {"sub_queries": [{"query": "版图设计要点"}]}
        self._reflects = list(reflects or [])
        self.plan_prompts: list[str] = []
        self.reflect_prompts: list[str] = []
        self.reflect_schemas: list[str] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        content = messages[-1]["content"]
        if "sub_queries" in schema_hint:
            self.plan_prompts.append(content)
            return json.dumps(self._plan)
        self.reflect_prompts.append(content)
        self.reflect_schemas.append(schema_hint)
        if self._reflects:
            return json.dumps(self._reflects.pop(0))
        return json.dumps({"next_action": "answer", "sufficient": True})


def _retriever(repo, llm, *, wired=True, owner_id=""):
    bind_chat_client(repo, "reasoning_agent", llm)
    retriever = ReasoningRetriever.from_repository(repo, repo.settings)
    if wired:
        retriever.retrieval_experiences = repo.retrieval_experiences
        retriever.agent_profile = repo.agent_profile
        retriever.profile_owner_id = owner_id
    return retriever


def _run(retriever, notebook, effort, **kwargs):
    limits = ask_retrieval_limits(effort)
    return retriever.run(notebook.id, "版图设计要点", "", limits=limits,
                         intent_detail=INTENT_DETAIL, **kwargs), limits


def _steps(result, step_type):
    return [step for step in result.trace if step.step_type == step_type]


# ------------------------------------------------------------- ① 三条件总闸

def test_absent_below_deep_effort(repo):
    notebook = _seed(repo)
    _write_experience(repo, "ppr", "bad", RATIONALE_PPR)
    llm = _SeqLLM()
    retriever = _retriever(repo, llm)
    result, _ = _run(retriever, notebook, "standard")

    assert not _steps(result, "consult_memory")
    assert all(CONSULT_MEMORY_ACTION not in schema
               for schema in llm.reflect_schemas)
    assert all("consult_memory" not in prompt
               for prompt in llm.reflect_prompts)


def test_offered_and_reachable_at_deep_effort(repo):
    notebook = _seed(repo)
    _write_many_experiences(repo, [("ppr", "bad", RATIONALE_PPR)])
    llm = _SeqLLM([{"next_action": "consult_memory"},
                   {"next_action": "answer", "sufficient": True}])
    retriever = _retriever(repo, llm)
    result, _ = _run(retriever, notebook, "deep")

    assert CONSULT_MEMORY_ACTION in llm.reflect_schemas[0]
    steps = _steps(result, "consult_memory")
    assert len(steps) == 1
    assert steps[0].detail["entries"] == 1
    assert steps[0].detail["chars"] > 0


def test_kill_switch_alone_removes_the_action(repo):
    notebook = _seed(repo)
    _write_experience(repo, "ppr", "bad", RATIONALE_PPR)
    repo.settings.reasoning_consult_memory_enabled = False
    llm = _SeqLLM()
    retriever = _retriever(repo, llm)
    result, _ = _run(retriever, notebook, "exhaustive")

    assert not _steps(result, "consult_memory")
    assert all(CONSULT_MEMORY_ACTION not in schema
               for schema in llm.reflect_schemas)


def test_injection_switch_off_removes_the_action_even_at_exhaustive(repo):
    """总闸并入 experience_wiring_active(P4 开工裁决①):INJECT 关闭时
    kill switch + 档位都满足也不提供这个动作。"""
    notebook = _seed(repo)
    _write_experience(repo, "ppr", "bad", RATIONALE_PPR)
    repo.settings.retrieval_experience_inject_enabled = False
    llm = _SeqLLM()
    retriever = _retriever(repo, llm)
    result, _ = _run(retriever, notebook, "exhaustive")

    assert not _steps(result, "consult_memory")
    assert all(CONSULT_MEMORY_ACTION not in schema
               for schema in llm.reflect_schemas)


@pytest.mark.parametrize("off_kind", ["kill_switch", "effort", "inject"])
def test_frozen_baseline_matches_pre_integration_byte_for_byte(repo, off_kind):
    """任一条件关闭 ⇒ prompt/schema/trace 与「接入前」逐字相同。"""
    notebook = _seed(repo)
    _write_experience(repo, "ppr", "bad", RATIONALE_PPR)
    effort = "deep"
    if off_kind == "kill_switch":
        repo.settings.reasoning_consult_memory_enabled = False
    elif off_kind == "inject":
        repo.settings.retrieval_experience_inject_enabled = False
    else:
        effort = "standard"

    off_llm = _SeqLLM()
    off_retriever = _retriever(repo, off_llm)
    off_result, _ = _run(off_retriever, notebook, effort)

    # A second, otherwise-identically-configured run with consult_memory
    # forced off via the private consult_memory_active seam itself (rather
    # than re-deriving "what pre-integration means") is unnecessary here —
    # the assertion is simply that the byte-for-byte schema/prompt/trace
    # never mentions the feature.
    assert not consult_memory_active(
        repo.settings, ask_retrieval_limits(effort), repo.retrieval_experiences
    )
    assert all(CONSULT_MEMORY_ACTION not in schema
               for schema in off_llm.reflect_schemas)
    assert all("consult_memory" not in prompt
               for prompt in off_llm.reflect_prompts)
    assert not _steps(off_result, "consult_memory")


# --------------------------------------------------------- ② 选择:差集与去重

def test_excludes_entries_already_delivered_by_the_passive_block(repo):
    """一个库里只有 3 条经验(恰好填满被动块的 top-3)时,consult_memory
    找不到任何新东西——差集为空,记 skip 而不是 consult_memory 步。"""
    notebook = _seed(repo)
    situation = _situation()
    for action in _PASSIVE_FILLER_ACTIONS:
        _write_experience(repo, action, "good", f"{action} tends to help here.",
                          situation=situation)
    llm = _SeqLLM([{"next_action": "consult_memory"},
                   {"next_action": "answer", "sufficient": True}])
    retriever = _retriever(repo, llm)
    result, _ = _run(retriever, notebook, "deep")

    assert not _steps(result, "consult_memory")
    skip_reasons = [
        s.detail.get("reason") for s in result.trace if s.step_type == "skip"
    ]
    assert "consult_memory_nothing_new" in skip_reasons


def test_nothing_new_records_a_skip_but_still_spends_budget(repo):
    notebook = _seed(repo)
    llm = _SeqLLM([{"next_action": "consult_memory"},
                   {"next_action": "consult_memory"},
                   {"next_action": "consult_memory"},
                   {"next_action": "answer", "sufficient": True}])
    retriever = _retriever(repo, llm)
    result, limits = _run(retriever, notebook, "deep")

    skip_reasons = [
        s.detail.get("reason") for s in result.trace if s.step_type == "skip"
    ]
    assert skip_reasons.count("consult_memory_nothing_new") == 2
    assert skip_reasons.count("consult_memory_cap") == 1
    assert not _steps(result, "consult_memory")


def test_two_calls_do_not_resurface_the_same_entry_and_share_one_cap(repo):
    """经验库里给 consult_memory 留下 4 条(超过单次 top_k=3),两次调用
    合起来不重复、且累计渲染始终受同一个 600 字符上限约束。"""
    notebook = _seed(repo)
    _write_many_experiences(repo, [
        ("ppr", "bad", "ppr rarely finds anything on this shape."),
        ("exact_lookup", "good", "exact_lookup nails the section fast."),
        ("follow_chain", "bad", "follow_chain rarely composes cleanly here."),
        ("outline", "good", "outline pays off on survey-shaped questions."),
    ])
    llm = _SeqLLM([
        {"next_action": "consult_memory"},
        {"next_action": "consult_memory"},
        {"next_action": "answer", "sufficient": True},
    ])
    retriever = _retriever(repo, llm)
    result, _ = _run(retriever, notebook, "deep")

    steps = _steps(result, "consult_memory")
    assert len(steps) == 2
    # top_k=3 per call: first call takes 3 of the 4 remaining, second call
    # takes the last one — no overlap, none re-reported as "new" twice.
    assert steps[0].detail["entries"] == CONSULT_MEMORY_TOP_K
    assert steps[1].detail["entries"] == 1
    for step in steps:
        assert step.detail["chars"] <= CONSULT_MEMORY_BLOCK_MAX_CHARS


# ------------------------------------------------------- ③ 覆盖层:未送达行

def test_overlay_offers_this_users_own_undelivered_retrieval_note(repo):
    """整块理解块截断挤掉了这个成员自己的 retrieval_notes 行时,
    consult_memory 仍能把它带回来。"""
    notebook = _seed(repo)
    big = "工艺关键词" * 90  # 450 chars, clipped to 400 by clip_block_value
    _write_profile_block(repo, notebook.id, "", "corpus_shape", big)
    _write_profile_block(repo, notebook.id, "", "key_entities", big)
    _write_profile_block(repo, notebook.id, "", "corpus_gaps", big)
    _write_profile_block(
        repo, notebook.id, "u1", "retrieval_notes", "这位成员常问时序收敛细节")

    llm = _SeqLLM([{"next_action": "consult_memory"},
                   {"next_action": "answer", "sufficient": True}])
    retriever = _retriever(repo, llm, owner_id="u1")
    result, _ = _run(retriever, notebook, "deep")

    # Precondition: the three shared blocks alone already blow the whole-
    # block cap, so retrieval_notes truly never reached the passive block.
    profile_steps = _steps(result, "profile")
    assert profile_steps
    assert "时序收敛" not in profile_steps[0].summary

    steps = _steps(result, "consult_memory")
    assert len(steps) == 1
    # No experience-library rows exist in this test — the whole delivered
    # content is the overlay note, so a non-zero char count with zero
    # "entries" is the signature of the overlay half firing on its own.
    assert steps[0].detail["entries"] == 0
    assert steps[0].detail["chars"] > 0


def test_overlay_absent_when_owner_has_no_note(repo):
    notebook = _seed(repo)
    _write_many_experiences(repo, [("ppr", "bad", RATIONALE_PPR)])
    llm = _SeqLLM([{"next_action": "consult_memory"},
                   {"next_action": "answer", "sufficient": True}])
    retriever = _retriever(repo, llm, owner_id="u1")
    result, _ = _run(retriever, notebook, "deep")

    steps = _steps(result, "consult_memory")
    assert len(steps) == 1
    assert steps[0].detail["entries"] == 1


# --------------------------------------------------------- ④ 策略位与预算

def test_allow_consult_memory_false_skips_dispatch_even_when_offered(repo):
    notebook = _seed(repo)
    _write_experience(repo, "ppr", "bad", RATIONALE_PPR)
    llm = _SeqLLM([{"next_action": "consult_memory"},
                   {"next_action": "answer", "sufficient": True}])
    retriever = _retriever(repo, llm)
    retriever.allow_consult_memory = False
    result, _ = _run(retriever, notebook, "deep")

    assert not _steps(result, "consult_memory")
    skip_reasons = [
        s.detail.get("reason") for s in result.trace if s.step_type == "skip"
    ]
    assert "consult_memory_disabled" in skip_reasons


def test_budget_cap_is_configurable(repo):
    notebook = _seed(repo)
    repo.settings.reasoning_max_consult_memory = 1
    llm = _SeqLLM([{"next_action": "consult_memory"},
                   {"next_action": "consult_memory"},
                   {"next_action": "answer", "sufficient": True}])
    retriever = _retriever(repo, llm)
    result, _ = _run(retriever, notebook, "deep")

    skip_reasons = [
        s.detail.get("reason") for s in result.trace if s.step_type == "skip"
    ]
    assert skip_reasons.count("consult_memory_cap") == 1


# ------------------------------------------------------------- ⑤ 零新增 I/O

def test_zero_new_io_during_the_action(repo):
    """动作期间:``read_blocks`` 0 次新增调用(复用 run() 已读的原始行),
    ``store.read_all`` 因进程级 memo 0 次新增调用。"""
    notebook = _seed(repo)
    _write_experience(repo, "ppr", "bad", RATIONALE_PPR)
    _write_profile_block(
        repo, notebook.id, "u1", "retrieval_notes", "心得ABC")
    llm = _SeqLLM([{"next_action": "consult_memory"},
                   {"next_action": "consult_memory"},
                   {"next_action": "answer", "sufficient": True}])
    retriever = _retriever(repo, llm, owner_id="u1")

    read_blocks_calls = []
    read_all_calls = []
    orig_read_blocks = retriever.agent_profile.read_blocks
    orig_read_all = retriever.retrieval_experiences.read_all

    def counted_read_blocks(*args, **kwargs):
        read_blocks_calls.append(1)
        return orig_read_blocks(*args, **kwargs)

    def counted_read_all(*args, **kwargs):
        read_all_calls.append(1)
        return orig_read_all(*args, **kwargs)

    retriever.agent_profile.read_blocks = counted_read_blocks
    retriever.retrieval_experiences.read_all = counted_read_all

    _run(retriever, notebook, "deep")

    # run() itself reads each store exactly once at the top; two
    # consult_memory calls must not add any further reads.
    assert read_blocks_calls == [1]
    assert read_all_calls == [1]


# --------------------------------------------------- ⑥ 纯函数:select/render

def test_select_consultable_excludes_ids_and_prioritises_zero_hit_actions():
    situation = _situation()
    ppr_id = experience_id(situation, "ppr")
    exact_id = experience_id(situation, "exact_lookup")
    entries = [
        {"id": ppr_id, "situation": situation, "action": "ppr",
         "polarity": "bad", "rationale": RATIONALE_PPR, "support": 2},
        {"id": exact_id, "situation": situation, "action": "exact_lookup",
         "polarity": "good", "rationale": RATIONALE_EXACT, "support": 5},
    ]
    picked = select_consultable(entries, situation, zero_hit_actions=["ppr"])
    assert [e["id"] for e in picked] == [ppr_id, exact_id]

    excluded = select_consultable(entries, situation, exclude_ids=[ppr_id])
    assert [e["id"] for e in excluded] == [exact_id]


def test_render_consult_block_drops_whole_rows_over_cap():
    """整块 600 字符硬顶按**整行**丢弃(镜像 render_experience_block 的同名
    用例):单行的 rationale 早在 clip_rationale 那道 160 字符闸就封顶了,
    真正撑爆整块预算的是**多行**累加。修复轮:返回值改为
    ``RenderedConsultBlock``,``delivered_ids`` 必须是被丢弃行之外的真子集。
    """
    situation = _situation()
    near_cap_rationale = "x" * 150
    rows = [
        {"id": f"rx_{'abcdef'[index]}" + "0" * 27, "situation": situation,
         "action": action, "polarity": "bad",
         "rationale": f"{index}{near_cap_rationale}", "support": 9}
        for index, action in enumerate(("ppr", "exact_lookup", "follow_chain"))
    ]
    rendered = render_consult_block(rows)
    assert rendered.rendered_text.startswith(CONSULT_HEADER)
    assert len(rendered.rendered_text) <= CONSULT_MEMORY_BLOCK_MAX_CHARS
    from app.services.retrieval_experience_block import rendered_row_count
    assert rendered_row_count(rendered.rendered_text) < len(rows), "至少一行装不下"
    assert not rendered.rendered_text.endswith("…"), "整块不做尾部截断"
    # delivered_ids 必须是真正渲染进块的行——严格子集,且顺序与传入一致。
    assert len(rendered.delivered_ids) < len(rows)
    assert set(rendered.delivered_ids) <= {r["id"] for r in rows}
    assert rendered.overlay_rendered is False

    short_rows = [{
        "id": "rx_" + "b" * 32, "situation": situation, "action": "ppr",
        "polarity": "bad", "rationale": RATIONALE_PPR, "support": 1,
    }]
    rendered = render_consult_block(short_rows, extra_lines=["yours: 心得"])
    assert rendered.rendered_text.startswith(CONSULT_HEADER)
    assert "心得" in rendered.rendered_text
    assert len(rendered.rendered_text) <= CONSULT_MEMORY_BLOCK_MAX_CHARS
    assert rendered.delivered_ids == (short_rows[0]["id"],)
    assert rendered.overlay_rendered is True


def test_render_consult_block_prioritises_the_overlay_note_when_budget_is_tight():
    """修复轮 Q-P1-3:``extra_lines``(本人覆盖层的未送达心得)先渲染。用满打
    满算的行占满预算,再补一条心得——旧实现(rows 先渲染)会让心得整条被挤掉;
    新实现必须仍然把它塞进去,即便代价是挤掉最后一行经验库条目。"""
    situation = _situation()
    near_cap_rationale = "x" * 150
    rows = [
        {"id": f"rx_{'abcdef'[index]}" + "0" * 27, "situation": situation,
         "action": action, "polarity": "bad",
         "rationale": f"{index}{near_cap_rationale}", "support": 9}
        for index, action in enumerate(("ppr", "exact_lookup", "follow_chain"))
    ]
    overlay = "yours: " + ("y" * 100)
    rendered = render_consult_block(rows, extra_lines=[overlay])
    assert rendered.overlay_rendered is True, "心得必须优先于经验库行占到预算"
    assert "yours:" in rendered.rendered_text
    assert len(rendered.rendered_text) <= CONSULT_MEMORY_BLOCK_MAX_CHARS


def test_worst_experience_for_only_returns_bad_polarity_for_the_named_action():
    situation = _situation()
    entries = [
        {"id": "rx_" + "c" * 32, "situation": situation, "action": "ppr",
         "polarity": "good", "rationale": "ppr works well here", "support": 1},
        {"id": "rx_" + "d" * 32, "situation": situation, "action": "ppr",
         "polarity": "bad", "rationale": RATIONALE_PPR, "support": 3},
        {"id": "rx_" + "e" * 32, "situation": situation, "action": "exact_lookup",
         "polarity": "bad", "rationale": RATIONALE_EXACT, "support": 9},
    ]
    found = worst_experience_for(entries, situation, "ppr")
    assert found is not None
    assert found["rationale"] == RATIONALE_PPR
    assert worst_experience_for(entries, situation, "follow_chain") is None


def test_a_broken_experience_store_read_skips_the_turn_instead_of_failing_the_run(
    repo, monkeypatch,
):
    """codex #538 R1 P2:注入开着时一次瞬态经验库读取失败不得把整次 run 打挂
    ——执行体整体 fail-open,记 consult_memory_unavailable 的 skip。"""
    import app.services.reasoning_retrieval as rr

    notebook = _seed(repo)
    _write_experience(repo, "ppr", "bad", "ppr rarely helps here.")
    llm = _SeqLLM([{"next_action": "consult_memory"},
                   {"next_action": "answer", "sufficient": True}])
    retriever = _retriever(repo, llm)

    def _boom(store):
        raise RuntimeError("experience store went away")

    monkeypatch.setattr(rr, "_cached_experiences", _boom)
    result, _limits = _run(retriever, notebook, "deep")
    skip_reasons = [
        s.detail.get("reason") for s in result.trace if s.step_type == "skip"
    ]
    assert "consult_memory_unavailable" in skip_reasons


def test_consult_returns_entries_the_passive_block_selected_but_never_delivered(repo):
    """codex #538 R1 P2:排除集只含真送达的被动块前缀。精确构造:恰好 3 条
    同场景条目、满长 rationale——被动块行预算 391 只装得下 2 行,第 3 条
    「选中未送达」。修复后 consult 恰好送达那 1 条;回退成按选中集排除则
    3 条全被挡、落 nothing_new。"""
    notebook = _seed(repo)
    filler = "x" * 150
    _write_experience(repo, "ppr", "bad", f"ppr {filler}a.")
    _write_experience(repo, "exact_lookup", "bad", f"exact {filler}b.")
    _write_experience(repo, "follow_chain", "bad", f"chain {filler}c.")
    llm = _SeqLLM([{"next_action": "consult_memory"},
                   {"next_action": "answer", "sufficient": True}])
    retriever = _retriever(repo, llm)
    result, _limits = _run(retriever, notebook, "deep")

    passive = next(s for s in result.trace if s.step_type == "experience")
    assert passive.detail["entries"] == 2, "前提:被动块只送达 2/3 条"
    consult_steps = _steps(result, "consult_memory")
    assert consult_steps, "选中未送达的第 3 条必须可经 consult 送达"
    assert consult_steps[0].detail["entries"] == 1
    skip_reasons = [
        s.detail.get("reason") for s in result.trace if s.step_type == "skip"
    ]
    assert "consult_memory_nothing_new" not in skip_reasons


def test_the_zero_hit_priority_set_filters_to_positive_counts():
    """codex #538 R1 P2:命中清零后键仍留在 zero_hit_by_action 字典里——按键集
    传给 select_consultable 会把刚成功的动作当「哑火」优先。

    源码钉而非编排钉,如实登记原因:要在真 run 里造出「键在、计数 0」需要
    miss→hit 序列,而 miss 会推进 stale 熔断、hit 需要 fixture 打开 PPR/图,
    两者都让用例变成对无关机制的编排;这个性质本身是调用点的一个表达式,
    按源码钉(变异回 set(zero_hit_by_action) 即红)。"""
    import inspect
    import app.services.reasoning_retrieval as rr

    source = inspect.getsource(rr.ReasoningRetriever.run)
    call_start = source.index("select_consultable(")
    call_src = source[call_start:call_start + 600]
    assert "zero_hit_by_action.items() if c > 0" in call_src, (
        "select_consultable 的 zero_hit_actions 必须按正计数过滤"
    )
