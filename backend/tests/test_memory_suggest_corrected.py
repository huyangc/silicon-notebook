"""SQLite corrected-signal contract and strict reply-field validation."""
from __future__ import annotations

import json
from itertools import product
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.model_schema import response_format_for_hint
from app.core.request_context import set_request_user
from app.services.memory_suggest_signals import SuggestReplyRejected, corrected_signals, parse_suggest_reply
from app.services.memory_suggest_wiring import memory_suggest_enabled_signals, memory_suggest_wiring_active
from app.services.prompts import MEMORY_SUGGEST_SCHEMA_HINT
from app.services.sqlite_repository import SQLiteRepository
from tests import memory_suggest_corrected_cases as cases
from tests.memory_suggest_job_cases import Env


@pytest.fixture
def env(tmp_path):
    repo = SQLiteRepository(Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}",
        storage_dir=str(tmp_path / 'storage'), _env_file=None,
        event_log_enabled=False, llm_log_enabled=False))
    database = repo._runtime.database
    def run(sql, params=()):
        with database.write(operation="test.seed") as db:
            db.execute(sql, params)
    def rows(sql, params=()):
        with database.connect() as db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]
    try:
        yield Env(runtime=repo._runtime, run=run, rows=rows, json=json.dumps)
    finally:
        set_request_user(None)
        repo.close()


@pytest.mark.parametrize("case", cases.CASES)
def test_corrected_contract(env, case):
    cases.CASES[case](env)


@pytest.mark.parametrize("flag", [None, 0, 1, "false", "true"])
def test_conflict_flag_requires_a_real_boolean(flag):
    with pytest.raises(SuggestReplyRejected, match="conflicts_with_sources_not_a_boolean"):
        parse_suggest_reply({"items": [{"item_id": "i1", "keep": False,
            "conflicts_with_sources": flag}]}, frozenset({"i1"}), frozenset(), frozenset())


@pytest.mark.parametrize("clarified,repeated,corrected", tuple(product([False, True], repeat=3)))
def test_all_three_class_gates_are_independent(clarified, repeated, corrected):
    settings = SimpleNamespace(auto_memory_suggest_enabled=True,
        auto_memory_suggest_clarified_enabled=clarified,
        auto_memory_suggest_repeated_enabled=repeated,
        auto_memory_suggest_corrected_enabled=corrected)
    expected = {signal for signal, active in zip(("clarified", "repeated", "corrected"), (clarified, repeated, corrected)) if active}
    assert memory_suggest_enabled_signals(settings) == expected
    assert memory_suggest_wiring_active(settings) == bool(expected)


@pytest.mark.parametrize("stamp", [None, "", "not-a-timestamp"])
def test_legacy_invalid_answer_time_does_not_claim_causal_correction(stamp):
    row = {"id": "later", "created_at": "2026-10-01T00:02:00+00:00", "question": "Correction clock source",
           "answer_id": "later-answer", "previous_answer_id": "prior-answer", "conversation_id": "conv"}
    projections = {"later-answer": {"conversation_id": "conv", "excerpt": "current"},
                   "prior-answer": {"conversation_id": "conv", "created_at": stamp, "excerpt": "prior"}}
    assert corrected_signals([row], projections, notebook_id="nb") == []


@pytest.mark.parametrize("previous,later,allowed", [
    ("2026-10-01T09:00:00.000200+01:00", "2026-10-01T08:00:00.000201+00:00", True),
    ("2026-10-01T09:00:00.000201+01:00", "2026-10-01T08:00:00.000200+00:00", False),
    ("2026-10-01T09:00:00.000201+01:00", "2026-10-01T08:00:00.000201+00:00", True),
])
def test_causal_filter_uses_exact_microseconds_across_offsets(previous, later, allowed):
    row = {"id": "later", "created_at": later, "question": "Correction clock source",
           "answer_id": "later-answer", "previous_answer_id": "prior-answer", "conversation_id": "conv"}
    projections = {"later-answer": {"conversation_id": "conv", "excerpt": "current"},
                   "prior-answer": {"conversation_id": "conv", "created_at": previous, "excerpt": "prior"}}
    assert bool(corrected_signals([row], projections, notebook_id="nb")) is allowed


def test_model_schema_requires_the_conflict_boolean_for_every_entry():
    schema = response_format_for_hint(MEMORY_SUGGEST_SCHEMA_HINT)["json_schema"]["schema"]
    entry = schema["properties"]["items"]["items"]
    # Shared hint projection makes nested scalars nullable; the domain parser
    # above still rejects null rather than treating it as a false decision.
    assert entry["properties"]["conflicts_with_sources"] == {"type": ["boolean", "null"]}
    assert "conflicts_with_sources" in entry["required"]
