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
    "unconfigured_model",
    # 「没有图」有两种,行为不同,两个都必须在这一组 oracle 里:
    #   no_kg          = 纯散文库(有文档、零可枚举元素、零知识对象)。文档本身是
    #                    可枚举集合之一,所以它**照常进循环**——用户问「库里有哪几篇」
    #                    时那份目录才是答案(codex R5 P1);
    #   no_collections = 零源库,三类计数全为零 ⇒ 早退那句「请先构建知识图谱」。
    "no_kg",
    "no_collections",
    # graph 这个 ask 模式退役时一并删除了三个案例:"graph"(引擎本身)、
    # "no_hits"(经 mode="graph" 制造的零命中早退)、"large_graph_refusal"
    # (大库拒绝全图漫游——该行为已随该模式从代码里消失)。见
    # generate_repository_contract_fixtures.py collect_ask_goldens() 的同条注释。
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
        assert response["mode"] in {"chunk", "reasoning"}, name
        assert isinstance(response["model_errors"], list), name


def test_ask_early_exit_flags_are_frozen():
    cases = _goldens()["cases"]

    assert cases["unconfigured_model"]["response"]["model_errors"]
    # 零源库:早退仍然发生(确定性兜底 + 那句明确提示)。
    no_collections = cases["no_collections"]["response"]
    assert no_collections["kg_required"] is True
    assert no_collections["llm_mode"] == "deterministic"
    assert "本笔记本尚未构建知识图谱" in no_collections["conclusion"]
    # 纯散文库:放行进循环(不是确定性兜底),但旗标仍如实为 True——放行只是不再
    # 阻断,不是把「这个库没有图」说成假的。
    prose_only = cases["no_kg"]["response"]
    assert prose_only["kg_required"] is True
    assert prose_only["llm_mode"] != "deterministic"
    assert "本笔记本尚未构建知识图谱" not in prose_only["conclusion"]


def test_current_repository_runtime_matches_the_frozen_ask_oracle():
    generated = _generator_module().collect_ask_goldens()
    assert generated == _goldens()


