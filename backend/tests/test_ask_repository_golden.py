from __future__ import annotations

import gc
import importlib.util
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    #   no_collections = 零源库,三类计数全为零 ⇒ 早退那句「没有可检索的来源」。
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
    # 文案说的是「当前笔记本没有可检索的来源」——图不再是**当前笔记本**作答的
    # 前提,零源才是。原文段落检索是 active-only(codex R2 P2-1),所以第二条出路
    # 只能是「挂载/整理一个已建知识图谱的参考库」:参考库靠图与清单参与,它的原文
    # 段落进不了这条通道。提到图是那条出路的真实条件,不是把图重新变成前提——
    # 这里钉的是后者不能回来。
    assert "当前笔记本没有可检索的来源" in no_collections["conclusion"]
    assert "尚未构建知识图谱" not in no_collections["conclusion"]
    assert "请先点" not in no_collections["conclusion"]
    # 纯散文库:放行进循环(不是确定性兜底),但旗标仍如实为 True——放行只是不再
    # 阻断,不是把「这个库没有图」说成假的。
    prose_only = cases["no_kg"]["response"]
    assert prose_only["kg_required"] is True
    assert prose_only["llm_mode"] != "deterministic"
    assert "没有可检索的来源" not in prose_only["conclusion"]

    # PR-3 起同步面也接了轨迹 sink,于是两条**早退**分支的响应必须带上那时已经
    # 落库的 intent 步(`streamed_pre_trace`)。`null` 是接 sink 之前的形态:
    # 响应否认一段它自己刚写进 `ask_trace_steps` 的轨迹,重开这条会话就会看到
    # 响应里没有的步骤。
    for case in ("unconfigured_model", "no_collections"):
        trace = cases[case]["response"]["reasoning_trace"]
        assert trace, f"{case}:早退响应必须带上已落库的那几步"
        assert trace[0]["step_type"] == "intent", case


def test_current_repository_runtime_matches_the_frozen_ask_oracle():
    generated = _generator_module().collect_ask_goldens()
    assert generated == _goldens()




def _app_code_reading(clock_owner) -> object:
    """A function whose module is ``app.*``, calling ``time.perf_counter``.

    ``_FixtureClock`` tells the replayed run's own reads apart from everyone
    else's by the caller's module, so the probes below need a caller that looks
    like application code without adding a module to ``app``.
    """
    namespace = {"__name__": "app.fixture_clock_probe", "clock_owner": clock_owner}
    exec("def read():\n    return clock_owner.perf_counter()\n", namespace)
    return namespace["read"]


def test_the_fixture_clock_advances_only_for_the_runs_own_reads():
    clock = _generator_module()._FixtureClock()
    holder = SimpleNamespace(perf_counter=clock)
    read = _app_code_reading(holder)

    with clock:
        assert read() == pytest.approx(1000.001)
        # Not application code (a gc callback, a test plugin, a library): the
        # real clock, and the fixture clock does not move.
        assert clock() != pytest.approx(1000.002)
        assert read() == pytest.approx(1000.002)

    # A thread that already existed when the run started is not the run's,
    # even when it runs application code.
    go, done = threading.Event(), threading.Event()
    seen: list[float] = []

    def leftover() -> None:
        go.wait(5)
        seen.append(read())
        done.set()

    early = threading.Thread(target=leftover, daemon=True)
    early.start()
    clock = _generator_module()._FixtureClock()
    holder.perf_counter = clock
    with clock:
        go.set()
        assert done.wait(5)
        early.join(5)
        assert seen[0] != pytest.approx(1000.001)

        # A thread the run starts (the model scheduler, a retrieval pool) is
        # the run's, and so is one that thread starts in turn.
        def nested() -> None:
            inner = threading.Thread(target=lambda: seen.append(read()))
            inner.start()
            inner.join(5)

        worker = threading.Thread(target=lambda: (seen.append(read()), nested()))
        worker.start()
        worker.join(5)
        assert seen[1:] == [pytest.approx(1000.001), pytest.approx(1000.002)]
        assert read() == pytest.approx(1000.003)


def test_a_thread_a_leftover_thread_starts_mid_run_is_not_the_runs():
    """A leftover that keeps spawning (a re-arming timer, a pool growing on a
    foreign submit) must not advance the clock through its new threads."""
    clock = _generator_module()._FixtureClock()
    holder = SimpleNamespace(perf_counter=clock)
    read = _app_code_reading(holder)
    go, done = threading.Event(), threading.Event()
    seen: list[float] = []

    def leftover() -> None:
        go.wait(5)
        child = threading.Thread(target=lambda: seen.append(read()))
        child.start()
        child.join(5)
        done.set()

    threading.Thread(target=leftover, daemon=True).start()
    with clock:
        go.set()
        assert done.wait(5)
        assert seen[0] != pytest.approx(1000.001)
        assert read() == pytest.approx(1000.001)


def test_reads_made_while_a_collection_runs_do_not_advance_the_clock():
    """A collection runs on whatever thread triggered it, the Ask's included;
    a finalizer it runs that reads the clock from application code is not part
    of the replayed run."""
    clock = _generator_module()._FixtureClock()
    holder = SimpleNamespace(perf_counter=clock)
    read = _app_code_reading(holder)
    finalized: list[float] = []

    class Cycle:
        def __init__(self) -> None:
            self.me = self

        def __del__(self) -> None:
            finalized.append(read())

    with clock:
        Cycle()
        gc.collect()
        assert len(finalized) == 1
        assert finalized[0] != pytest.approx(1000.001)
        assert read() == pytest.approx(1000.001)
    assert gc.callbacks.count(clock._on_gc) == 0


def test_the_ask_oracle_ignores_clock_reads_from_outside_the_run():
    """The oracle holds while other code in the worker reads the clock.

    Reproduces both ways an xdist worker used to perturb the frozen
    ``duration_ms`` values: a ``gc.callbacks`` hook reading ``perf_counter``
    whenever a collection fires mid-Ask (a low threshold makes that happen on
    every run), and a thread left over from an earlier test reading it from
    application code.
    """
    stop = threading.Event()
    read = _app_code_reading(time)

    def leftover() -> None:
        while not stop.is_set():
            read()
            time.sleep(0.0005)

    def on_gc(_phase, _info) -> None:
        time.perf_counter()

    thread = threading.Thread(target=leftover, daemon=True)
    thresholds = gc.get_threshold()
    thread.start()
    gc.callbacks.append(on_gc)
    gc.set_threshold(100)
    try:
        generated = _generator_module().collect_ask_goldens()
    finally:
        gc.set_threshold(*thresholds)
        gc.callbacks.remove(on_gc)
        stop.set()
        thread.join(5)
    assert generated == _goldens()
