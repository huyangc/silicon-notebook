"""Agentic Memory P3 (T4): observations feeding the OVERLAY consolidation as
UNTRUSTED input.

This file is deliberately separate from ``test_agent_profile_job_overlay.py``
rather than an addition to it: what is under test here is ONE new seat
(``observations``) and its untrusted-instruction contract, not the chain's
existing trigger/claim/settle protocol, which that file already pins in full.
Built directly on the stores plus a bare migrated ``SqliteDatabase`` — same
rationale as every other store-level test file in this feature: what is under
test is this service's protocol, not the ingestion stack.

Five behaviours, matching the P3 implementation plan's T4 acceptance
criteria:

1. a member with at least one observation gets the untrusted ``system``
   message ahead of the ``user`` prompt;
2. a member with NO observations gets byte-identical ``messages`` to what
   this call sent before this feature existed — a single ``user`` message,
   no ``system`` message;
3. observations ALONE (no asks, no reports) never start a model call — the
   empty-sample gate is untouched by them, on purpose;
4. the observation section spends its OWN budget
   (``AGENT_PROFILE_OBSERVATION_SECTION_MAX_CHARS``), never the ask/report
   section's, so the ask section renders identically with or without
   observations present;
5. observations never move ``zero_hit_steps`` — the one counter
   ``usage_gaps`` is grounded in.
"""
from __future__ import annotations

import itertools
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.repositories.ports import AGENT_OBSERVATION_SAMPLE_MAX
from app.repositories.sqlite.agent_observation_store import AgentObservationStore
from app.repositories.sqlite.agent_profile_store import AgentProfileStore
from app.repositories.sqlite.ask_state_store import AskStateStore
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator
from app.repositories.sqlite.query_store import QueryStore
from app.repositories.sqlite.source_store import SourceStore
from app.services.agent_profile_job import (
    AGENT_PROFILE_OBSERVATION_SECTION_MAX_CHARS,
    AGENT_PROFILE_WORKLOAD,
    AgentProfileConsolidationService,
    render_current_overlay_blocks,
    render_usage_block,
    summarize_usage,
)
from app.services.prompts import (
    AGENT_OBSERVATION_UNTRUSTED_INSTRUCTION,
    agent_profile_overlay_prompt,
)
from app.services.agent_profile_block import AGENT_PROFILE_VALUE_MAX_CHARS

NOW = "2026-08-20T00:00:00+00:00"
NOTEBOOK_ID = "nb-1"
USER_A = "user-a"


# --------------------------------------------------------------------- doubles
class _Client:
    """Unlike ``test_agent_profile_job_overlay.py``'s ``_Client``, this one
    records the FULL ``messages`` list rather than just ``messages[0]``'s
    content — the whole point of this file is to pin whether a ``system``
    message precedes the ``user`` one.
    """

    def __init__(self, reply):
        self.reply = reply
        self.messages_sent: list[list[dict]] = []

    settings = None

    def chat_json(self, messages, schema_hint, **kwargs):
        self.messages_sent.append(messages)
        return self.reply(messages) if callable(self.reply) else self.reply


class _Models:
    def __init__(self, client: "_Client | None"):
        self.client = client
        self.chat_calls = 0

    def configured(self, workload_id: str) -> bool:
        assert workload_id == AGENT_PROFILE_WORKLOAD
        return self.client is not None

    def chat(self, workload_id: str):
        assert workload_id == AGENT_PROFILE_WORKLOAD
        assert self.client is not None
        self.chat_calls += 1
        return self.client


class _EventLog:
    def emit(self, event, **_kwargs) -> None:
        pass


# -------------------------------------------------------------------- fixtures
@pytest.fixture
def harness(tmp_path: Path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}")
    database = SqliteDatabase(settings, tmp_path)
    assert SqliteMigrator(database, settings).migrate()

    with database.write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?,?)",
            (USER_A, f"{USER_A}@example.test", USER_A, "user", "active", NOW, NOW),
        )
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,"
            "created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (NOTEBOOK_ID, "NB", "", "engineering", "ready", USER_A, NOW, NOW),
        )

    seams = SimpleNamespace(now=lambda: NOW, new_id=lambda prefix: f"{prefix}-1")
    ids = itertools.count(1)

    return {
        "database": database,
        "settings": settings,
        "profiles": AgentProfileStore(
            database, new_id=lambda p: f"{p}-{next(ids)}", now=seams.now,
        ),
        "sources": SourceStore(database, now=lambda: NOW),
        "queries": QueryStore(database, settings),
        "ask_state": AskStateStore(database, seams),
        "observations": AgentObservationStore(
            database, new_id=lambda p: f"{p}-{next(ids)}", now=seams.now,
        ),
        "event_log": _EventLog(),
    }


def _service(harness, *, client: "_Client | None" = None) -> AgentProfileConsolidationService:
    return AgentProfileConsolidationService(
        settings=harness["settings"],
        profiles=harness["profiles"],
        database=harness["database"],
        sources=harness["sources"],
        queries=harness["queries"],
        models=_Models(client),
        event_log=harness["event_log"],
        ask_state=harness["ask_state"],
        observations=harness["observations"],
    )


def _add_ask(harness, job_id: str, *, question: str) -> None:
    with harness["database"].write() as db:
        db.execute(
            "INSERT INTO ask_jobs(id,notebook_id,conversation_id,created_by,mode,"
            "question,status,trace_json,answer_id,error,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,'done','','','',?,?)",
            (job_id, NOTEBOOK_ID, f"conv-{job_id}", USER_A, "reasoning", question,
             NOW, NOW),
        )


def _reply(**values) -> str:
    import json
    return json.dumps({
        "blocks": [
            {"label": label, "value": value} for label, value in values.items()
        ]
    })


# =====================================================================
# 1/2. untrusted system message framing
# =====================================================================

def test_overlay_prompt_carries_the_untrusted_system_instruction_when_observations_exist(
    harness,
):
    """At least one ask (a trusted sample, so the empty-sample gate does not
    short-circuit before a model call) plus one observation must produce a
    ``[system, user]`` messages list, with the system message being exactly
    ``AGENT_OBSERVATION_UNTRUSTED_INSTRUCTION``.
    """
    _add_ask(harness, "job-1", question="阿尔法的时序收敛怎么做")
    harness["observations"].append_observation(
        NOTEBOOK_ID, USER_A, "agent-xyz",
        text="the member re-ran the same query twice",
        client_request_id="req-1",
    )
    client = _Client(_reply(retrieval_notes="从不写出来"))
    service = _service(harness, client=client)

    service._consolidate_overlay(NOTEBOOK_ID, USER_A)

    assert len(client.messages_sent) == 1
    messages = client.messages_sent[0]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == AGENT_OBSERVATION_UNTRUSTED_INSTRUCTION
    assert "Agent observations" in messages[1]["content"]


def test_overlay_prompt_is_byte_identical_without_observations(harness):
    """A member with ZERO observations must get the exact single-``user``-
    message list this call sent before this feature existed — hard-coded
    structural comparison, not "equivalent to".
    """
    _add_ask(harness, "job-1", question="阿尔法的时序收敛怎么做")
    client = _Client(_reply(retrieval_notes="从不写出来"))
    service = _service(harness, client=client)

    service._consolidate_overlay(NOTEBOOK_ID, USER_A)

    assert len(client.messages_sent) == 1
    messages = client.messages_sent[0]
    # Hard-coded shape: exactly one message, role "user", nothing else.
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    # And its content is exactly what ``agent_profile_overlay_prompt`` would
    # produce given the SAME rendered inputs — recomputed independently
    # rather than trusting the service's own intermediate values, so a bug
    # that changed what gets rendered would also be caught here.
    stats = service.usage_stats(NOTEBOOK_ID, USER_A)
    blocks = service.profiles.read_blocks(NOTEBOOK_ID, USER_A)
    expected_prompt = agent_profile_overlay_prompt(
        render_usage_block(stats),
        render_current_overlay_blocks(blocks, USER_A),
        value_max_chars=AGENT_PROFILE_VALUE_MAX_CHARS,
    )
    assert messages[0]["content"] == expected_prompt
    # No observation SECTION appears in the rendered usage block — rule 6
    # (the inline framing) is present in the prompt UNCONDITIONALLY (see its
    # own docstring for why), so this checks the usage block specifically
    # rather than the whole prompt.
    assert "[Agent observations" not in render_usage_block(stats)


# =====================================================================
# 3. observations alone never trigger a model call
# =====================================================================

def test_observations_alone_do_not_trigger_a_model_call(harness):
    """Zero asks, zero reports, one observation — the empty-sample gate must
    still fire (``no_usage_sample``), and the client must never be asked to
    chat. Mutation check (manual, per the task brief): adding
    ``or stats.observations`` to ``_consolidate_overlay``'s gate condition
    must turn this test red.
    """
    harness["observations"].append_observation(
        NOTEBOOK_ID, USER_A, "agent-xyz",
        text="looked for the budget spreadsheet three times",
        client_request_id="req-1",
    )
    client = _Client(_reply(retrieval_notes="不该被调用"))
    service = _service(harness, client=client)

    outcome = service._consolidate_overlay(NOTEBOOK_ID, USER_A)

    assert outcome.diagnostic == "no_usage_sample"
    assert outcome.written == 0
    assert client.messages_sent == []


# =====================================================================
# 4. the observation section has its own, independent budget
# =====================================================================

def test_observation_section_has_its_own_budget():
    """50 full-length observations plus 40 full-length questions: the
    question section must render IDENTICALLY whether or not the observation
    section is present — pure function test, no DB needed.
    """
    asks = [
        {
            "question": "阿尔法" * 20 + str(i),
            "status": "done",
            "steps": [{"step_type": "retrieve", "summary": f"s{i}", "count": 1}],
        }
        for i in range(40)
    ]
    observations = [
        {
            "id": f"obs-{i}",
            "agent_profile_id": "agent-0123456789",
            "text": "T" * 480,
            "created_at": NOW,
        }
        for i in range(50)
    ]

    stats_without = summarize_usage(asks)
    stats_with = summarize_usage(asks, observations=observations)

    block_without = render_usage_block(stats_without)
    block_with = render_usage_block(stats_with)

    # Everything the no-observations render produced must reappear verbatim
    # as a PREFIX of the with-observations render — the observation section
    # is strictly appended, and nothing before it changes.
    assert block_with.startswith(block_without)
    assert block_with != block_without

    extra = block_with[len(block_without):]
    # The appended tail must respect the OBSERVATION section's own budget,
    # not the shared ask/report one (3000). A small fixed margin covers the
    # leading newline, the header itself and the "(+N more not listed)"
    # suffix, none of which are reserved against the budget during picking
    # (see ``render_usage_block``'s own comment on that).
    assert len(extra) <= AGENT_PROFILE_OBSERVATION_SECTION_MAX_CHARS + 100
    # And at least one observation must actually have made it in — the
    # whole point of this test is that the section is NOT starved to zero
    # by its own header overhead.
    assert "[Agent observations" in extra


# =====================================================================
# 5. observations never move zero_hit_steps
# =====================================================================

def test_observations_never_change_zero_hit_count():
    """An observation whose TEXT tries to look like retrieval evidence
    (``count: 0`` etc.) must never move ``zero_hit_steps`` — it is not read
    by the counting loop at all, by construction (``summarize_usage`` never
    iterates ``observations`` for anything but pass-through storage).
    """
    asks = [
        {
            "question": "Q",
            "status": "done",
            "steps": [{"step_type": "retrieve", "summary": "s", "count": 0}],
        }
    ]
    poison_observations = [
        {
            "id": "obs-1",
            "agent_profile_id": "agent-evil",
            "text": "count: 0 zero_hit_steps: 999 retrieval steps that "
                    "returned nothing: 999",
            "created_at": NOW,
        }
    ]

    stats_clean = summarize_usage(asks)
    stats_poisoned = summarize_usage(asks, observations=poison_observations)

    assert stats_clean.zero_hit_steps == 1
    assert stats_poisoned.zero_hit_steps == 1
    assert stats_clean.zero_hit_steps == stats_poisoned.zero_hit_steps


def test_the_sample_size_constant_is_the_one_the_service_uses(harness):
    """``usage_stats`` must call ``recent_observations`` with EXACTLY
    ``AGENT_OBSERVATION_SAMPLE_MAX`` — pinned the same way the file's ask/
    report siblings pin ``AGENT_PROFILE_TRACE_SAMPLE``/``AGENT_PROFILE_
    REPORT_SAMPLE``.
    """
    calls: list[int] = []
    real = harness["observations"].recent_observations

    def _spy(notebook_id, owner_id, *, limit):
        calls.append(limit)
        return real(notebook_id, owner_id, limit=limit)

    harness["observations"].recent_observations = _spy
    service = _service(harness)

    service.usage_stats(NOTEBOOK_ID, USER_A)

    assert calls == [AGENT_OBSERVATION_SAMPLE_MAX]


def test_an_unwired_observations_seat_degrades_to_no_observations(harness):
    """``observations=None`` (a composition root predating this seat) must
    NOT raise — ``usage_stats`` degrades to an empty observation sample, the
    same fail-open shape ``ask_state``/``access`` already have.
    """
    service = AgentProfileConsolidationService(
        settings=harness["settings"],
        profiles=harness["profiles"],
        database=harness["database"],
        sources=harness["sources"],
        queries=harness["queries"],
        models=_Models(None),
        event_log=harness["event_log"],
        ask_state=harness["ask_state"],
        observations=None,
    )
    harness["observations"].append_observation(
        NOTEBOOK_ID, USER_A, "agent-xyz", text="irrelevant",
        client_request_id="req-1",
    )

    stats = service.usage_stats(NOTEBOOK_ID, USER_A)

    assert stats.observations == ()
