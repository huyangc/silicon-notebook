"""Agentic Memory P4 (T6): step-level zero-hit experience nudge.

Server-driven (not model-driven — contrast with ``consult_memory`` in
``test_consult_memory_action.py``): after one of the four tracked action
branches (ppr/exact_lookup/expand/follow_chain) comes back with zero new
results ``_ZERO_HIT_NUDGE_THRESHOLD`` times in one run, the NEXT reflect
round's prompt gets one deterministic sentence quoting a matching "bad"
experience-library entry, up to ``_ZERO_HIT_NUDGE_MAX_PER_RUN`` times per run.

Pure-function coverage of ``_zero_hit_nudge_note`` (threshold, ordering,
dedup-by-nudged-action, "no matching entry" fallthrough) lives in the first
half; the second half wires it through a real ``ReasoningRetriever.run()`` to
prove the splice point, the gate (INJECT switch only — NOT effort-tiered,
unlike consult_memory), and fail-open behaviour.
"""
from __future__ import annotations

import json

import pytest

from app.core.ask_retrieval_policy import ask_retrieval_limits
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.reasoning_retrieval import (
    _ZERO_HIT_NUDGE_MAX_PER_RUN,
    _ZERO_HIT_NUDGE_THRESHOLD,
    _ZERO_HIT_TRACKED_ACTIONS,
    ReasoningRetriever,
    _zero_hit_nudge_note,
)
from app.services.retrieval_experience_projection import (
    current_situation,
    experience_id,
)
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client


NOW = "2026-08-20T00:00:00+08:00"

INTENT_DETAIL = {
    "resolved_question": "版图设计要点有哪些",
    "result_scope": "ranked",
    "completeness_required": False,
    "retrieval_effort": "standard",
    "entities": ["版图"],
    "constraints": [],
    "excluded_topics": [],
    "assumptions": [],
    "expected_output": "要点清单",
    "mandatory_topics": ["版图设计要点"],
}

RATIONALE_PPR = "PPR breadth rarely pays off on this single-document shape."
RATIONALE_FOLLOW_CHAIN = "follow_chain rarely composes on a fact-only library."


def _situation(**overrides) -> dict:
    situation = current_situation(
        INTENT_DETAIL, mode="reasoning", retrieval_effort="standard")
    situation.update(overrides)
    return situation


def _bad_entry(action, rationale, *, situation=None, entry_id=None):
    situation = situation or _situation()
    return {
        "id": entry_id or experience_id(situation, action),
        "situation": situation, "action": action,
        "polarity": "bad", "rationale": rationale, "support": 3,
    }


# ------------------------------------------------ ① 纯函数:_zero_hit_nudge_note

def test_below_threshold_no_nudge():
    situation = _situation()
    entries = [_bad_entry("ppr", RATIONALE_PPR, situation=situation)]
    assert _zero_hit_nudge_note(
        {"ppr": _ZERO_HIT_NUDGE_THRESHOLD - 1}, set(), entries, situation
    ) is None


def test_at_threshold_returns_action_and_embeds_rationale_verbatim():
    situation = _situation()
    entries = [_bad_entry("ppr", RATIONALE_PPR, situation=situation)]
    picked = _zero_hit_nudge_note(
        {"ppr": _ZERO_HIT_NUDGE_THRESHOLD}, set(), entries, situation)
    assert picked is not None
    action, note = picked
    assert action == "ppr"
    assert RATIONALE_PPR in note, "rationale 必须原样嵌入(P4 开工裁决⑤)"
    assert "ppr_retrieve" in note, "面向模型的措辞用动作 id 不用存储词"


def test_no_matching_bad_entry_falls_through_to_the_next_tracked_action():
    situation = _situation()
    # ppr crosses the threshold but has no "bad" entry; exact_lookup does.
    entries = [_bad_entry("exact_lookup", "exact_lookup rarely helps here.",
                          situation=situation)]
    picked = _zero_hit_nudge_note(
        {"ppr": 5, "exact_lookup": _ZERO_HIT_NUDGE_THRESHOLD},
        set(), entries, situation)
    assert picked is not None
    assert picked[0] == "exact_lookup"


def test_already_nudged_action_is_skipped_even_past_threshold():
    situation = _situation()
    entries = [_bad_entry("ppr", RATIONALE_PPR, situation=situation)]
    picked = _zero_hit_nudge_note(
        {"ppr": _ZERO_HIT_NUDGE_THRESHOLD * 3}, {"ppr"}, entries, situation)
    assert picked is None


def test_no_qualifying_action_returns_none():
    assert _zero_hit_nudge_note({}, set(), [], _situation()) is None


def test_good_polarity_entry_never_qualifies():
    situation = _situation()
    entries = [{
        "id": experience_id(situation, "ppr"), "situation": situation,
        "action": "ppr", "polarity": "good",
        "rationale": "ppr does great here.", "support": 3,
    }]
    picked = _zero_hit_nudge_note(
        {"ppr": _ZERO_HIT_NUDGE_THRESHOLD}, set(), entries, situation)
    assert picked is None


def test_deterministic_scan_order_matches_the_tracked_action_tuple():
    """Two actions both qualify: the picked one is the FIRST in
    ``_ZERO_HIT_TRACKED_ACTIONS`` order, not the one with the higher count."""
    situation = _situation()
    assert _ZERO_HIT_TRACKED_ACTIONS.index("expand") > (
        _ZERO_HIT_TRACKED_ACTIONS.index("ppr"))
    entries = [
        _bad_entry("expand", "expand rarely surfaces anything new.",
                   situation=situation),
        _bad_entry("ppr", RATIONALE_PPR, situation=situation),
    ]
    picked = _zero_hit_nudge_note(
        {"expand": _ZERO_HIT_NUDGE_THRESHOLD + 10,
         "ppr": _ZERO_HIT_NUDGE_THRESHOLD},
        set(), entries, situation)
    assert picked[0] == "ppr"


# --------------------------------------------------------------- ② 端到端接线

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
    instance.settings.graph_ppr_enabled = True
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
        object_id = db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=?",
            (notebook.id,),
        ).fetchone()[0]
    repo.collection_catalog.invalidate()
    return notebook, object_id


def _write_experience(repo, action, polarity, rationale, *, situation=None):
    situation = situation or _situation()
    entry_id = experience_id(situation, action)
    repo.retrieval_experiences.upsert_experience(
        entry_id, situation=situation, action=action, polarity=polarity,
        rationale=rationale, provenance=["run-1"], provenance_max=60,
        replace_conclusion=True,
    )
    _reset_memo()
    return entry_id


class _SeqLLM:
    configured = True

    def __init__(self, reflects=None, plan=None):
        self._plan = plan or {"sub_queries": [{"query": "版图设计要点"}]}
        self._reflects = list(reflects or [])
        self.plan_prompts: list[str] = []
        self.reflect_prompts: list[str] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        content = messages[-1]["content"]
        if "sub_queries" in schema_hint:
            self.plan_prompts.append(content)
            return json.dumps(self._plan)
        self.reflect_prompts.append(content)
        if self._reflects:
            return json.dumps(self._reflects.pop(0))
        return json.dumps({"next_action": "answer", "sufficient": True})


def _retriever(repo, llm):
    bind_chat_client(repo, "reasoning_agent", llm)
    retriever = ReasoningRetriever.from_repository(repo, repo.settings)
    retriever.retrieval_experiences = repo.retrieval_experiences
    return retriever


def _run(retriever, notebook, effort, **kwargs):
    limits = ask_retrieval_limits(effort)
    return retriever.run(notebook.id, "版图设计要点", "", limits=limits,
                         intent_detail=INTENT_DETAIL, **kwargs)


def test_nudge_appears_after_two_zero_hit_exact_lookups(repo):
    notebook, _oid = _seed(repo)
    _write_experience(repo, "exact_lookup",
                      "bad", "exact_lookup rarely finds this shape.")
    llm = _SeqLLM([
        {"next_action": "exact_lookup", "exact_term": "set_db"},
        {"next_action": "exact_lookup", "exact_term": "get_db"},
        {"next_action": "answer", "sufficient": True},
    ])
    retriever = _retriever(repo, llm)
    result = _run(retriever, notebook, "standard")

    # Two genuinely zero-hit exact_lookup steps (different terms so neither
    # is skipped as a duplicate).
    exact_steps = [s for s in result.trace if s.step_type == "exact_lookup"]
    assert len(exact_steps) == 2
    assert all(s.detail["found"] == 0 for s in exact_steps)

    # ⚠ The raw rationale text ALSO appears from round 1 onward via the
    # PASSIVE experience block (select_experiences does not discriminate by
    # polarity) — that block is not what this test is about. The NUDGE
    # marker ("已连续") only exists in ``_zero_hit_nudge_note``'s own wording,
    # so it is the only string that proves THIS mechanism fired.
    assert len(llm.reflect_prompts) == 3
    assert llm.reflect_prompts[0].count("已连续") == 0
    assert llm.reflect_prompts[1].count("已连续") == 0
    assert llm.reflect_prompts[2].count("已连续") == 1
    assert "exact_lookup rarely finds this shape." in llm.reflect_prompts[2]


def test_nudge_is_not_gated_by_effort_tier(repo):
    """consult_memory 只在 deep 及以上档;这条提示没有档位闸——standard 档
    照样生效。"""
    notebook, _oid = _seed(repo)
    _write_experience(repo, "exact_lookup",
                      "bad", "exact_lookup rarely finds this shape.")
    llm = _SeqLLM([
        {"next_action": "exact_lookup", "exact_term": "set_db"},
        {"next_action": "exact_lookup", "exact_term": "get_db"},
        {"next_action": "answer", "sufficient": True},
    ])
    retriever = _retriever(repo, llm)
    _run(retriever, notebook, "overview")

    assert any("已连续" in prompt for prompt in llm.reflect_prompts)


def test_injection_switch_off_disables_the_nudge(repo):
    notebook, _oid = _seed(repo)
    _write_experience(repo, "exact_lookup",
                      "bad", "exact_lookup rarely finds this shape.")
    repo.settings.retrieval_experience_inject_enabled = False
    llm = _SeqLLM([
        {"next_action": "exact_lookup", "exact_term": "set_db"},
        {"next_action": "exact_lookup", "exact_term": "get_db"},
        {"next_action": "exact_lookup", "exact_term": "run_db"},
        {"next_action": "answer", "sufficient": True},
    ])
    retriever = _retriever(repo, llm)
    _run(retriever, notebook, "standard")

    assert all("已连续" not in prompt for prompt in llm.reflect_prompts)


def test_max_two_nudges_per_run_across_different_actions(repo):
    notebook, object_id = _seed(repo)
    # Every dispatched action in this test is a deliberate zero-hit probe, so
    # the "no progress" stale circuit breaker (default limit 3) would cut the
    # sequence short well before exact_lookup gets its turn — raise it so all
    # six scripted actions actually run.
    repo.settings.reasoning_stale_limit = 10
    _write_experience(repo, "ppr", "bad", RATIONALE_PPR)
    _write_experience(repo, "follow_chain", "bad", RATIONALE_FOLLOW_CHAIN)
    _write_experience(repo, "exact_lookup",
                      "bad", "exact_lookup rarely finds this shape.")
    llm = _SeqLLM([
        # ppr zero-hit twice (no edges in this notebook).
        {"next_action": "ppr_retrieve", "ppr_query": "毫不相关的检索词一"},
        {"next_action": "ppr_retrieve", "ppr_query": "毫不相关的检索词二"},
        # follow_chain zero-hit twice, varying direction so neither call is
        # treated as a duplicate of the other.
        {"next_action": "follow_chain",
         "follow_chain": {"start_object_id": object_id, "direction": "out"}},
        {"next_action": "follow_chain",
         "follow_chain": {"start_object_id": object_id, "direction": "in"}},
        # exact_lookup zero-hit twice — a third tracked action that would
        # ALSO qualify, proving the run-level cap (not a per-action cap).
        {"next_action": "exact_lookup", "exact_term": "set_db"},
        {"next_action": "exact_lookup", "exact_term": "get_db"},
        {"next_action": "answer", "sufficient": True},
    ])
    retriever = _retriever(repo, llm)
    _run(retriever, notebook, "standard")

    # ppr crosses the threshold first (round 2), then follow_chain (round 4)
    # — that is exactly ``_ZERO_HIT_NUDGE_MAX_PER_RUN`` (2) events, so
    # exact_lookup (which only crosses its own threshold at round 6, after
    # the run-level cap is already spent) must NEVER be nudged. Marker
    # phrases are action-id-specific so a raw-rationale substring check
    # (confounded by the passive block also rendering these same rationales
    # from round 1 onward) cannot be used here.
    full_transcript = "\n".join(llm.reflect_prompts)
    assert full_transcript.count("已连续") == _ZERO_HIT_NUDGE_MAX_PER_RUN == 2
    assert "「ppr_retrieve」这类动作在当前场景已连续" in full_transcript
    assert "「follow_chain」这类动作在当前场景已连续" in full_transcript
    assert "「exact_lookup」这类动作在当前场景已连续" not in full_transcript


def test_fail_open_on_a_broken_experience_store(repo):
    """读库炸了不该打断这条 run——只是没有提示,不记 skip 步。"""
    notebook, _oid = _seed(repo)

    class _BrokenStore:
        def version_signal(self):
            raise RuntimeError("boom")

        def read_all(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    llm = _SeqLLM([
        {"next_action": "exact_lookup", "exact_term": "set_db"},
        {"next_action": "exact_lookup", "exact_term": "get_db"},
        {"next_action": "answer", "sufficient": True},
    ])
    retriever = _retriever(repo, llm)
    retriever.retrieval_experiences = _BrokenStore()

    result = _run(retriever, notebook, "standard")
    assert result is not None
    assert not any(
        s.step_type == "skip"
        and str(s.detail.get("reason") or "").startswith("experience")
        for s in result.trace
    )


def test_cancellation_during_the_nudge_lookup_still_propagates(repo):
    from app.services.cancellation import AskCancelled

    notebook, _oid = _seed(repo)

    class _CancellingStore:
        def version_signal(self):
            raise AskCancelled("user pressed stop")

    llm = _SeqLLM([
        {"next_action": "exact_lookup", "exact_term": "set_db"},
        {"next_action": "exact_lookup", "exact_term": "get_db"},
        {"next_action": "answer", "sufficient": True},
    ])
    retriever = _retriever(repo, llm)
    retriever.retrieval_experiences = _CancellingStore()

    with pytest.raises(AskCancelled):
        _run(retriever, notebook, "standard")
