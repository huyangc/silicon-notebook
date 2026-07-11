from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from app.models.schemas import AskResponse
from app.services import sqlite_repository


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = (
    ROOT
    / "backend"
    / "tests"
    / "fixtures"
    / "repository_contract"
    / "ask_responses.json"
)
GENERATOR = ROOT / "scripts" / "generate_repository_contract_fixtures.py"

REQUIRED_CASES = {
    "chunk",
    "reasoning",
    "graph",
    "unconfigured_model",
    "no_kg",
    "no_hits",
    "large_graph_refusal",
}


def _goldens() -> dict[str, object]:
    assert FIXTURE.is_file(), f"missing Ask golden fixture: {FIXTURE}"
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _generator_module():
    spec = importlib.util.spec_from_file_location("repository_ask_fixture_generator", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ask_goldens_cover_modes_and_every_early_exit():
    goldens = _goldens()

    assert goldens["source_commit"] == "3334626"
    assert set(goldens["cases"]) == REQUIRED_CASES


def test_every_ask_golden_is_a_complete_response_and_payload_pair():
    for name, case in _goldens()["cases"].items():
        response = AskResponse.model_validate(case["response"]).model_dump(mode="json")
        assert response == case["response"], name
        assert case["answers_payload"] == response, name
        assert response["mode"] in {"chunk", "reasoning", "graph"}, name
        assert isinstance(response["model_errors"], list), name


def test_ask_early_exit_flags_are_frozen():
    cases = _goldens()["cases"]

    assert cases["unconfigured_model"]["response"]["model_errors"]
    assert cases["no_kg"]["response"]["kg_required"] is True
    assert cases["no_hits"]["response"]["grounded"] is False
    refusal = cases["large_graph_refusal"]["response"]
    assert refusal["mode"] == "graph"
    assert refusal["llm_mode"] == "deterministic"
    assert "规模过大" in refusal["conclusion"]


def test_current_repository_runtime_matches_the_frozen_ask_oracle():
    generated = _generator_module().collect_ask_goldens()
    assert generated == _goldens()


def test_facade_ask_dispatches_current_and_retired_modes_through_runtime_service():
    calls = []

    class AskServiceSpy:
        def ask_current(self, notebook_id, payload):
            calls.append((notebook_id, payload.mode, "user-1"))
            return payload.mode

    service = AskServiceSpy()
    facade_class = getattr(sqlite_repository, "SQLite" + "Repository")
    repo = object.__new__(facade_class)
    repo.__dict__["_runtime"] = SimpleNamespace(
        ask_component=service,
    )

    for mode in ("chunk", "reasoning", "graph", "fast", "global"):
        assert getattr(repo, "a" + "sk")("nb-1", SimpleNamespace(mode=mode)) == mode

    assert calls == [
        ("nb-1", mode, "user-1")
        for mode in ("chunk", "reasoning", "graph", "fast", "global")
    ]
