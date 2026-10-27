"""``ReasoningRetriever.run``: the reflect loop and its close, scenario by scenario.

Each scenario drives one real run (real SQLite repository and ports, a scripted
model whose reflect payloads pass the production shape gate) through a
deliberate path of the loop, and asserts the user/model-visible facts it exists
for (``Scenario.expect``): which skip reason a branch reports, which budget it
spends, what the next reflect turn is told, what the run returns or raises.
Together the scenarios reach every skip branch of the loop and all four entry
shapes (plain ``run``, Report, Ask's ``run_stage``, knowhow completion);
``test_scenarios_reach_every_loop_branch_whatever_the_shell_exports`` keeps
that true.

The byte-level snapshots that served as the oracle while ``run`` was split
into methods (debt plan S1) were retired with the last split PR; what the
scenarios assert is the long-term contract, not how the loop spells it.

Wall clock: no assertion depends on it. The loop's only clock-driven branches
— the chunk-rerank deadline and the plugin action timeout — are not covered.
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from app.core.ask_retrieval_policy import ask_retrieval_limits
from app.core.config import Settings
from app.domain.reflect_action import (
    ReflectActionDescriptor,
    ReflectActionItem,
    ReflectActionOutcome,
    ReflectActionParameter,
    ReflectActionSpec,
)
from app.models.schemas import NotebookCreate
from app.services.model_registry import WORKLOADS
from app.services.reasoning_retrieval import ReasoningRetriever, ReflectDecision
from app.services.retrieval_experience_projection import (
    current_situation,
    experience_id,
)
from app.services.sqlite_repository import SQLiteRepository
from tests import reasoning_loop_probe as rig
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client

NOW = "2026-10-12T00:00:00+08:00"
OWNER = "u-owner"
QUESTION = "版图设计要点有哪些"

INTENT_DETAIL = {
    "resolved_question": QUESTION,
    "result_scope": "ranked",
    "completeness_required": False,
    "retrieval_effort": "deep",
    "entities": ["版图"],
    "constraints": ["先进工艺"],
    "excluded_topics": ["封装"],
    "assumptions": [],
    "expected_output": "要点清单",
    "mandatory_topics": ["版图设计要点"],
}

ANSWER = {"next_action": "answer", "sufficient": True, "reason": "证据够了"}


# --------------------------------------------------------------- scripting


def act(action: str, **fields) -> dict:
    payload = {"next_action": action, "reason": f"选 {action}"}
    payload.update(fields)
    return payload


def sub(query: str, **fields) -> dict:
    request = {"query": query, "types": [], "prefer": "balanced", "reason": "补查"}
    request.update(fields)
    return act("add_subquery", new_sub_query=request)


def chain(start: str, edge_type: str = "derived_from", direction: str = "out"):
    return act("follow_chain", follow_chain={
        "start_object_id": start, "edge_type": edge_type,
        "direction": direction})


def outline(*sections, sufficient: bool = False, action: str = "update_outline"):
    payload = act(action, outline_sections=list(sections))
    if sufficient:
        payload["sufficient"] = True
    return payload


@dataclass
class RawDecision:
    """A decision the parser could never produce (unknown or gated-off
    action): handed to the loop directly, without a model call."""

    next_action: str


@dataclass
class Effect:
    """Run ``apply(context)`` when the model is asked, then answer ``then``."""

    apply: Callable[[dict], None]
    then: Any


def _resolve(item, context, prompt):
    if isinstance(item, Effect):
        item.apply(context)
        return _resolve(item.then, context, prompt)
    if callable(item):
        return _resolve(item(context, prompt), context, prompt)
    if isinstance(item, str) and item.startswith("@id:"):
        return context["ids"][item[4:]]
    if isinstance(item, dict):
        return {key: _resolve(value, context, prompt) for key, value in item.items()}
    if isinstance(item, list):
        return [_resolve(value, context, prompt) for value in item]
    return item


_SHOWN_ID = re.compile(r"\(id=(ko-[0-9a-f]+|chunk-\d+|el-\d+)\)")


def shown_ids(prompt: str) -> List[str]:
    """Candidate ids in the order this reflect prompt shows them."""
    seen: List[str] = []
    for key in _SHOWN_ID.findall(prompt):
        if key not in seen:
            seen.append(key)
    return seen


def section_with_shown(section_id, title, start, stop, **extra):
    """An outline section bound to the ``start:stop`` slice of shown ids."""

    def build(context, prompt):
        section = {"id": section_id, "title": title,
                   "evidence": shown_ids(prompt)[start:stop]}
        section.update(extra)
        return section

    return build


class _Script(rig.ChatScript):
    def answer(self, workload, messages, schema_hint):
        if "next_action" in schema_hint and self.reflects:
            prompt = messages[-1]["content"]
            self.reflects[0] = _resolve(self.reflects[0], self.context, prompt)
        return super().answer(workload, messages, schema_hint)


# ----------------------------------------------------------- the notebook


def _source(db, notebook_id, source_id, title):
    db.execute(
        "INSERT INTO sources (id,notebook_id,title,source_type,status,"
        "parse_status,summary,created_at,updated_at) "
        "VALUES (?,?,?,'markdown','extracted','extracted','',?,?)",
        (source_id, notebook_id, title, NOW, NOW))


def _node(local_id, name, object_type="concept"):
    return {"local_id": local_id, "object_type": object_type,
            "payload": {"name": name, "section_path": "1"}, "evidence": []}


def _edge(src, tgt, edge_type, quote=None):
    return {"source_local_id": src, "target_local_id": tgt,
            "edge_type": edge_type,
            "evidence": [{"quote": quote}] if quote else []}


_PASSAGES = (
    ("set_db", "set_db 命令用于设置版图数据库的属性,是时序分析前的必要步骤。"),
    ("report_timing", "report_timing 输出关键路径的时序报告,用于版图设计签核。"),
    ("get_db", "get_db 读取版图数据库对象的属性值。"),
    ("read_lef", "read_lef 读入工艺 LEF 文件,定义版图设计用到的层与单元。"),
    ("overview", "版图设计要兼顾寄生电容与工艺角,常见做法是分层布线。"),
)
_PROCESS_NOTES = (
    "工艺说明开头:先进工艺下的版图约束。",
    "工艺说明中段:工艺角与寄生提取的关系。",
    "工艺说明结论:签核前必须复核工艺角。",
)
HUB_RULES = tuple(f"规则{index:02d}" for index in range(1, 31))


def _seed_graph(repo) -> tuple:
    """One notebook, three sources: a small concept graph, a two-hop
    ``derived_from`` chain, a 30-spoke hub (neighbor truncation, >6 weak
    edges), five identifier-named passages (exact lookup) with their elements,
    and a second document's elements (two document reads).

    Every KG name is unique. Two objects with the same name both take the
    exact-name score of 1.0, and the order of an exact tie is decided by
    floating-point noise upstream (it was measured to flip between BLAS
    builds); everything else in this fixture is separated by far more than
    that noise."""
    lifecycle = repo._runtime.knowledge_lifecycle
    notebook = repo._runtime.catalog.create_notebook(NotebookCreate(name="版图"))
    nb = notebook.id
    with repo._write() as db:
        _source(db, nb, "src-1", "版图手册")
        _source(db, nb, "src-2", "工艺说明")
        _source(db, nb, "src-3", "签核指南")
        for index, (name, text) in enumerate(_PASSAGES):
            chunk_id, element_id = f"chunk-{index}", f"el-{index}"
            section = f"命令 > {name}"
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
                "element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
                (chunk_id, nb, "src-1", text, section,
                 json.dumps([element_id]), NOW))
            db.execute(
                "INSERT INTO chunks_fts(chunk_id,notebook_id,text) VALUES (?,?,?)",
                (chunk_id, nb, text))
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,"
                "location_label,text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
                (element_id, "src-1", "paragraph", section, text,
                 json.dumps({"section_path": section}), NOW))
        for index, text in enumerate(_PROCESS_NOTES):
            label = f"第{index + 1}节"
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,"
                "location_label,text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
                (f"el-p{index}", "src-2", "paragraph", label, text,
                 json.dumps({"section_path": label}), NOW))
    nodes = [
        _node("A", "版图设计"), _node("B", "寄生电容"), _node("C", "工艺角"),
        _node("P", "前提甲", "claim"), _node("Q", "桥接乙", "claim"),
        _node("R", "结论丙", "claim"), _node("H", "版图规则"),
    ] + [_node(f"H{index}", name) for index, name in enumerate(HUB_RULES)]
    edges = [
        _edge("A", "B", "kind_of"), _edge("A", "C", "part_of"),
        _edge("P", "Q", "derived_from", "前提甲直接推出桥接乙"),
        _edge("Q", "R", "derived_from", "桥接乙直接推出结论丙"),
        _edge("A", "H", "depends_on"),
    ] + [_edge("H", f"H{index}", "part_of") for index in range(len(HUB_RULES))]
    lifecycle.store_kg(nb, "src-1", nodes, edges)
    lifecycle.rebuild_unified_kg(nb)
    repo.collection_catalog.invalidate()
    ids: Dict[str, str] = {}
    with repo._connect() as db:
        for row in db.execute(
            "SELECT id, payload FROM knowledge_objects WHERE notebook_id=? "
            "AND source_id='src-1' ORDER BY id", (nb,)
        ):
            ids.setdefault(json.loads(row["payload"]).get("name"), row["id"])
    return nb, ids


def _seed_empty(repo) -> tuple:
    notebook = repo._runtime.catalog.create_notebook(NotebookCreate(name="空库"))
    return notebook.id, {}


#: A flat numbered specification (in-document navigation): a virtual ``5.7.2``,
#: two ``Notes`` leaves (an ambiguous title) and a second source with no chunks
#: (an empty outline). No vectors: the section-hint block stays fail-open off.
_SECTION_ROWS = (
    ("c-s0", "5 Electrical", "第五章 电气规范概述"),
    ("c-s1", "5.7 Channel", "通道规范概述"),
    ("c-s2", "5.7.2.1 Loss and Crosstalk Mask", "插损与串扰掩模限值"),
    ("c-s3", "5.7.3 Standard Package", "标准封装插损表 5-18 数值"),
    ("c-s4", "Notes", "注释一"),
    ("c-s5", "6 Protocol", "协议层说明"),
    ("c-s6", "Notes", "注释二"),
)


def _seed_sections(repo) -> tuple:
    notebook = repo._runtime.catalog.create_notebook(NotebookCreate(name="规范"))
    nb = notebook.id
    with repo._write() as db:
        _source(db, nb, "src-spec", "互连规范")
        _source(db, nb, "src-blank", "空白文档")
        for chunk_id, path, text in _SECTION_ROWS:
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
                "element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
                (chunk_id, nb, "src-spec", f"[{path}] {text}", path, "[]", NOW))
    repo.collection_catalog.invalidate()
    return nb, {}


def _situation(effort: str) -> dict:
    return current_situation(INTENT_DETAIL, mode="reasoning", retrieval_effort=effort)


def _seed_experiences(repo, effort: str, entries) -> None:
    """``(action, polarity, rationale, support[, effort])``: an entry may name
    the effort of the situation it was learned in (default: the scenario's)."""
    store = repo._runtime.retrieval_experiences
    for action, polarity, rationale, support, *learned_in in entries:
        situation = _situation(learned_in[0] if learned_in else effort)
        store.upsert_experience(
            experience_id(situation, action, ""),
            situation=situation, action=action, polarity=polarity,
            rationale=rationale,
            provenance=[f"run-{action}-{index}" for index in range(support)],
            provenance_max=60, replace_conclusion=True, notebook_id="")


def _seed_profile(repo, notebook_id, blocks) -> None:
    for owner, label, value in blocks:
        repo._runtime.agent_profile.write_block(
            notebook_id, owner, label, value=value, evidence=[],
            expected_revision=0, origin="job", actor="")


# ------------------------------------------------------------ the plugin

PLUGIN_SPEC = ReflectActionSpec("acme.search", "acme_index", ReflectActionDescriptor(
    name="search_papers",
    description="Search an external paper index and return matching abstracts.",
    source_label="外部论文库",
    parameters=(
        ReflectActionParameter(name="query", description="what to look for",
                               kind="text", required=True),
    ),
    max_calls_per_run=2,
))


class _PluginHost:
    """Fixed topology, one scripted outcome; reports the deterministic half
    of every call (the deadline is wall-clock and left out)."""

    def __init__(self):
        self.outcomes = [ReflectActionOutcome((
            ReflectActionItem("外部论文 1", "外部摘录:先进工艺版图寄生提取",
                              "https://example.org/p1", "§1"),
        ), "", "", False)]

    def specs(self, deadline_monotonic, *, cancellation=None, event_sink=None):
        probe = rig.current_probe()
        if probe is not None:
            probe.note_port("plugin.specs")
        return (PLUGIN_SPEC,)

    def invoke(self, spec, call):
        probe = rig.current_probe()
        if probe is not None:
            probe.note_port("plugin.invoke " + json.dumps(
                {"action": spec.descriptor.name, "question": call.question,
                 "arguments": dict(call.arguments)}, ensure_ascii=False, sort_keys=True))
        return self.outcomes.pop(0) if self.outcomes else ReflectActionOutcome()


# -------------------------------------------------------------- scenarios


@dataclass
class Scenario:
    name: str
    reflects: List[Any]
    expect: Callable[["Observed"], None]
    question: str = QUESTION
    entry: str = "run"                   # run | report | ask | knowhow
    effort: Optional[str] = "standard"
    max_steps: Optional[int] = None
    top_n: Optional[int] = None
    intent_queries: tuple = ()
    intent_detail: Optional[dict] = None
    plan_queries: tuple = ("版图设计",)
    settings: dict = field(default_factory=dict)
    retrieval_overrides: Optional[Callable[[dict], dict]] = None
    experience_overrides: Optional[Callable[[dict], dict]] = None
    notebook: str = "graph"
    memory: bool = False                 # wire experiences + profile (run entry)
    experiences: tuple = ()
    profile: tuple = ()
    scope_sources: Optional[tuple] = None
    allow: dict = field(default_factory=dict)


class Observed:
    """The recorded run plus helpers the behaviour assertions read."""

    def __init__(self, data: dict):
        self.data = data
        self.trace = data["trace"]
        self.reflect_inputs = data["reflect_inputs"]

    def steps(self, step_type=None):
        return [s for s in self.trace if step_type is None or s["step_type"] == step_type]

    def reasons(self):
        return [s["detail"].get("reason") for s in self.trace if s["step_type"] == "skip"]

    def reflect_details(self):
        return [s["detail"] for s in self.steps("reflect")]

    def summaries(self):
        return [entry["candidates_summary"] for entry in self.reflect_inputs]

    def port_calls(self, prefix):
        ports = self.data["ports"]
        return [label for label in ports["main_thread"] + ports["worker_threads"]
                if label.startswith(prefix)]

    def port_count(self, prefix):
        """Calls on the run's own thread (the loop never fans out)."""
        return sum(1 for label in self.data["ports"]["main_thread"]
                   if label.startswith(prefix))

    @property
    def result(self):
        return self.data["result"]

    @property
    def raised(self):
        return self.data["outcome"].get("raised")


def _settings_env_names() -> frozenset:
    """Every environment name a ``Settings`` field can be read from (its alias
    and its field name; the lookup is case-insensitive)."""
    names = set()
    for name, info in Settings.model_fields.items():
        names.add(name.lower())
        if isinstance(info.validation_alias, str):
            names.add(info.validation_alias.lower())
    return frozenset(names)


#: The only process-level configuration a scenario runs with; everything else
#: is the shipped default plus the scenario's own ``settings`` overrides.
_SCENARIO_ENV = {
    "SILICON_NOTEBOOK_ENV_FILE": "",
    "MODEL_SERVICES_CONFIG": "",
    "EXTENSIONS_CONFIG": "",
    "LLM_LOG_ENABLED": "false",
    "LLM_CACHE_ENABLED": "false",
    "EMBED_DIM": "16",
}


def _make_repo(tmp_path, monkeypatch):
    # No local .env, and no deployment variable exported by the developer's
    # shell or the test process, may reach a recording.
    names = _settings_env_names()
    for key in list(os.environ):
        if key.lower() in names:
            monkeypatch.delenv(key)
    for key, value in _SCENARIO_ENV.items():
        monkeypatch.setenv(key, value)
    # Clearing also removed the shared isolation fixture's four paths (it sets
    # exactly these); point every one of them back under this test's tmp_path
    # so no worker writes into the checkout or a sibling worker's files.
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'loop.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("LLM_LOG_PATH", str(tmp_path / "logs" / "llm.jsonl"))
    repo = SQLiteRepository(Settings(_env_file=None))
    bind_all_embedding_clients(repo, rig.CountingEmbedder(dim=16))
    return repo


def _clear_process_caches():
    from app.repositories.sqlite import knowledge_counts_cache
    from app.services import reasoning_retrieval

    with reasoning_retrieval._EXPERIENCE_CACHE_LOCK:
        reasoning_retrieval._EXPERIENCE_CACHE.clear()
    # every memo in the module (``_MEMO`` and its three siblings): scenarios
    # share one deterministic notebook id, so a warm entry from the previous
    # scenario would otherwise decide whether this one issues the query.
    knowledge_counts_cache.invalidate()


def _bind_models(repo, script):
    for workload_id, workload in WORKLOADS.items():
        if workload.kind == "chat":
            bind_chat_client(repo, workload_id, rig.ScriptedChat(workload_id, script))


def _install_raw_decisions(retriever, script):
    inner = retriever.reflect

    def reflect(question, candidates_summary, *args, **kwargs):
        if script.reflects and isinstance(script.reflects[0], RawDecision):
            raw = script.reflects.pop(0)
            return ReflectDecision(next_action=raw.next_action, reason="raw")
        return inner(question, candidates_summary, *args, **kwargs)

    retriever.reflect = reflect


def _build_retriever(scenario, repo, cancel_event):
    if scenario.entry == "ask":
        retriever = repo._runtime.ask_component._build_reasoning_retriever(
            cancel_event=cancel_event, user_id=OWNER,
            egress_question=scenario.question)
        retriever.reflect_action_host = _PluginHost()
        return retriever
    if scenario.entry == "report":
        retrieval = repo.retrieval
        return ReasoningRetriever(
            retrieval=retrieval, model_clients=repo,
            communities=retrieval.community_queries(),
            settings=repo.settings, cancel_event=cancel_event,
            agent_profile=repo._runtime.agent_profile, profile_owner_id=OWNER,
            retrieval_experiences=repo._runtime.retrieval_experiences)
    if scenario.entry == "knowhow":
        retriever = ReasoningRetriever.from_repository(
            repo, repo.settings, cancel_event, fail_closed=True)
        retriever.allow_community_expansion = False
        retriever.allow_ppr = False
        retriever.allow_exact_lookup = False
        retriever.allow_enumeration = False
        retriever.allow_consult_memory = False
        retriever.allow_search_chunks = False
        retriever.allow_document_read = False
        retriever.allow_section_nav = False
        retriever.untrusted_evidence = True
        return retriever
    retriever = ReasoningRetriever.from_repository(repo, repo.settings, cancel_event)
    if scenario.memory:
        retriever.retrieval_experiences = repo._runtime.retrieval_experiences
        retriever.agent_profile = repo._runtime.agent_profile
        retriever.profile_owner_id = OWNER
    return retriever


def _invoke(scenario, repo, retriever, nb, on_step, cancel_event):
    limits = ask_retrieval_limits(scenario.effort) if scenario.effort else None
    if scenario.entry == "ask":
        return _invoke_stage(scenario, repo, retriever, nb, on_step, cancel_event, limits)
    if scenario.entry == "knowhow":
        return retriever.run(nb, scenario.question, top_n=12, max_steps=6)
    if scenario.entry == "report":
        return retriever.run(
            nb, scenario.question, on_step=on_step, max_steps=scenario.max_steps,
            limits=limits,
            intent_queries=([scenario.question, *scenario.intent_queries]
                            if scenario.intent_queries else None),
            intent_detail=scenario.intent_detail)
    return retriever.run(
        nb, scenario.question, "", on_step=on_step, top_n=scenario.top_n,
        max_steps=scenario.max_steps,
        intent_queries=list(scenario.intent_queries) or None,
        limits=limits, intent_detail=scenario.intent_detail)


def _invoke_stage(scenario, repo, retriever, nb, on_step, cancel_event, limits):
    from app.application.ask_reasoning import (
        ReasoningIntentProjection,
        ReasoningRetrievalRuntime,
        ReasoningRunInput,
    )
    from app.services.retrieval_run import retrieval_run

    detail = scenario.intent_detail
    stage = ReasoningRunInput(
        notebook_id=nb, question=scenario.question, history="",
        top_n=None, max_steps=None,
        intent_queries=tuple(scenario.intent_queries), limits=limits,
        intent=ReasoningIntentProjection(
            resolved_question=detail["resolved_question"],
            result_scope=detail["result_scope"],
            completeness_required=detail["completeness_required"],
            retrieval_effort=scenario.effort,
            entities=tuple(detail["entities"]),
            constraints=tuple(detail["constraints"]),
            excluded_topics=tuple(detail["excluded_topics"]),
            assumptions=tuple(detail["assumptions"]),
            expected_output=detail["expected_output"],
            mandatory_topics=tuple(detail["mandatory_topics"]),
        ),
    )
    captured = {}
    inner_run = retriever.run

    def run(*args, **kwargs):
        captured["result"] = inner_run(*args, **kwargs)
        return captured["result"]

    retriever.run = run
    with retrieval_run(run_kind="ask_reasoning", actor_id=OWNER,
                       cancel_event=cancel_event) as active:
        runtime = ReasoningRetrievalRuntime(
            scope=None, retrieval_run=active, cancellation=cancel_event,
            # the same connection-hold probe Ask hands the stage
            trace_sink=on_step, connection_probe=repo._runtime.database)
        retriever.run_stage(stage, runtime)
    return captured["result"]


def observe(scenario: Scenario, tmp_path, monkeypatch) -> dict:
    from app.services.source_scope import source_scope_context

    _clear_process_caches()
    with rig.deterministic_ids():
        repo = _make_repo(tmp_path, monkeypatch)
        for key, value in scenario.settings.items():
            setattr(repo.settings, key, value)
        seed = {"empty": _seed_empty, "sections": _seed_sections}.get(
            scenario.notebook, _seed_graph)
        nb, ids = seed(repo)
        if scenario.experiences:
            _seed_experiences(repo, scenario.effort, scenario.experiences)
        if scenario.profile:
            _seed_profile(repo, nb, scenario.profile)
        _clear_process_caches()
        cancel_event = threading.Event()
        context = {"ids": ids, "notebook_id": nb, "cancel": cancel_event,
                   "fault": False}
        script = _Script(
            plan={"sub_queries": [{"query": q} for q in scenario.plan_queries]},
            reflects=list(scenario.reflects), context=context)
        _bind_models(repo, script)
        probe = rig.Probe()
        streamed: List[Any] = []
        retriever = _build_retriever(scenario, repo, cancel_event)
        for name, value in scenario.allow.items():
            setattr(retriever, name, value)
        overrides = (scenario.retrieval_overrides(context)
                     if scenario.retrieval_overrides else {})
        retriever.retrieval = rig.PortProbe(retriever.retrieval, "retrieval", overrides)
        if retriever.retrieval_experiences is not None:
            retriever.retrieval_experiences = rig.PortProbe(
                retriever.retrieval_experiences, "experiences",
                scenario.experience_overrides(context)
                if scenario.experience_overrides else {})
        if retriever.agent_profile is not None:
            retriever.agent_profile = rig.PortProbe(retriever.agent_profile, "profile")
        # the remaining seats the loop reads storage through
        for seat in ("collection_catalog", "collection_enumeration", "sources",
                     "communities"):
            if getattr(retriever, seat) is not None:
                setattr(retriever, seat, rig.PortProbe(getattr(retriever, seat), seat))
        if retriever.source_generation is not None:
            retriever.source_generation = rig.recorded_callable(
                retriever.source_generation, "source_generation")
        # raw decisions inside, recorder outside: every turn's summary is
        # recorded, including the turns that never reach the model
        _install_raw_decisions(retriever, script)
        rig.install_reflect_recorder(retriever, probe)
        outcome: Dict[str, Any] = {}
        result = None
        with rig.probing(probe):
            probe.active = True
            try:
                if scenario.scope_sources:
                    with source_scope_context(nb, {
                        "mode": "include",
                        "source_ids": list(scenario.scope_sources),
                    }):
                        result = _invoke(scenario, repo, retriever, nb,
                                         streamed.append, cancel_event)
                else:
                    result = _invoke(scenario, repo, retriever, nb,
                                     streamed.append, cancel_event)
                outcome["returned"] = True
            except Exception as exc:  # noqa: BLE001 — recorded as behaviour
                outcome["raised"] = f"{type(exc).__name__}: {exc}"
            finally:
                probe.active = False
        trace = result.trace if result is not None else streamed
        observation = {
            "outcome": outcome,
            "trace": rig.trace_projection(trace),
            "on_step_is_trace": (
                scenario.entry == "knowhow"
                or [id(s) for s in streamed] == [id(s) for s in trace]),
            "reflect_inputs": probe.reflect_inputs,
            "model_calls": probe.model_calls,
            "ports": probe.split(probe.ports),
            "embeddings": dict(sorted(probe.embeddings.items())),
            "result": rig.result_projection(result) if result is not None else None,
        }
    return json.loads(json.dumps(observation, ensure_ascii=False))


SCENARIOS: List[Scenario] = []


def scenario(**kwargs):
    def register(expect):
        SCENARIOS.append(Scenario(expect=expect, **kwargs))
        return expect
    return register


# --- Report path: depth above the tier's step cap ---------------------------

@scenario(
    name="report_depth_above_tier_cap",
    entry="report", effort="overview", max_steps=8,
    reflects=[sub(f"版图补充问题 {index}") for index in range(1, 9)],
)
def _expect_report_depth(obs):
    # overview caps the loop at 4 steps even though the section asked for 8.
    assert len(obs.steps("reflect")) == 4
    assert obs.data["outcome"] == {"returned": True}
    assert obs.data["on_step_is_trace"]


# --- Ask path: run_stage with the production wiring -------------------------

@scenario(
    name="ask_stage_channels",
    entry="ask", effort="deep", intent_detail=INTENT_DETAIL,
    intent_queries=(QUESTION,),
    reflects=[
        act("enumerate_elements", enumerate={"collection": "sources"}),
        act("read_document", read_document={"source": "版图手册"}),
        act("read_document", read_document={"source": "工艺说明"}),
        act("search_chunks", query="版图 寄生电容"),
        act("search_papers", search_papers={"query": "版图寄生提取"}),
        act("ppr_retrieve", query="版图设计"),
        act("expand_community", query="@id:版图设计"),
        ANSWER,
    ],
)
def _expect_ask_stage(obs):
    assert [s["detail"]["next_action"] for s in obs.steps("reflect")] == [
        "enumerate_elements", "read_document", "read_document", "search_chunks",
        "search_papers", "ppr_retrieve", "expand_community"]
    details = obs.reflect_details()
    # the plugin turn brought only external material: stale held, not bumped
    assert details[5]["stale"] == details[4]["stale"] > 0
    assert obs.reasons()[-1] == "stale_circuit_breaker"
    assert obs.steps("plugin_action")
    assert obs.port_count("plugin.invoke") == 1
    assert obs.result["external_evidence"]
    # both reads reach the result, in the order they were made
    assert [read["source_id"] for read in obs.result["document_reads"]] == [
        "src-1", "src-2"]


# --- Knowhow completion entry: limits=None, fail_closed, top_n=12 -----------

@scenario(
    name="knowhow_completion_entry",
    entry="knowhow", effort=None,
    plan_queries=("前提甲 结论丙",),
    settings={"reasoning_per_query_limit": 30},
    # a narrow first round, so the later sub-query has >20 new ids to report
    retrieval_overrides=lambda ctx: {
        "federated_retrieve": _narrow_for(ctx, "前提甲 结论丙", 3)},
    reflects=[
        chain("@id:前提甲"),
        sub("规则"),
        RawDecision("enumerate_elements"),
        act("exact_lookup", query="set_db"),
        act("expand_community", query="@id:版图设计"),
        ANSWER,
    ],
)
def _expect_knowhow_entry(obs):
    assert obs.data["outcome"] == {"returned": True}
    reasons = obs.reasons()
    assert "exact_lookup_disabled" in reasons
    assert "community_expansion_disabled" in reasons
    assert "enumeration_disabled" in reasons
    assert obs.steps("follow_chain")[0]["detail"]["count"] == 1
    assert obs.steps("answer")[-1]["detail"]["top_n"] == 12
    added = [s for s in obs.steps("retrieve") if s["summary"].startswith("补充子查询")]
    assert added[0]["detail"]["result_ids_truncated"] is True
    assert all(c["roles"][0] == "system" for c in obs.data["model_calls"]
               if c["workload"] == "reasoning_agent")


@scenario(
    name="knowhow_follow_chain_raises",
    entry="knowhow", effort=None, plan_queries=("前提甲 结论丙",),
    retrieval_overrides=lambda ctx: {"follow_chain": _raise(RuntimeError("图读取失败"))},
    reflects=[chain("@id:前提甲"), ANSWER],
)
def _expect_knowhow_raise(obs):
    # fail_closed: the follow_chain failure escapes instead of failing open
    assert obs.raised == "RuntimeError: 图读取失败"
    assert obs.result is None
    assert len(obs.reflect_inputs) == 1
    assert obs.port_count("retrieval.follow_chain") == 1


def _narrow_for(ctx, query, keep):
    def factory(inner):
        def federated(notebook_id, text, *args, **kwargs):
            hits = inner(notebook_id, text, *args, **kwargs)
            return hits[:keep] if text == query else hits
        return federated
    return ("wrap", factory)


def _raise(exc):
    def fail(*_args, **_kwargs):
        raise exc
    return fail


# --- expand_graph -----------------------------------------------------------

@scenario(
    name="expand_graph_branches",
    plan_queries=("版图规则",),
    settings={"reasoning_neighbor_expand_limit": 25},
    reflects=[
        act("expand_graph", expand={"object_id": "@id:版图规则"}),
        act("expand_graph", expand={"object_id": "@id:版图规则"}),
        act("expand_graph", expand={"object_id": ""}),
        # one expanded node only: the "already expanded" ledger line joins a
        # set, so with two nodes its order would follow the string hash seed
        RawDecision("teleport"),
        ANSWER,
    ],
)
def _expect_expand_graph(obs):
    expands = obs.steps("expand")
    assert expands[0]["detail"]["neighbor_truncated"] is True
    assert expands[0]["detail"]["neighbor_limit"] == 25
    assert expands[0]["detail"]["result_ids_truncated"] is True
    assert len(expands[0]["detail"]["result_ids"]) == 20
    assert obs.reasons().count("empty_or_visited") == 2
    # the unknown action ends the loop: the scripted ANSWER is never asked for,
    # and the loop leaves before the turn's progress accounting (no stale bump,
    # no breaker) -- the close follows the unknown turn directly
    assert obs.steps("reflect")[-1]["detail"]["next_action"] == "teleport"
    assert [s["step_type"] for s in obs.steps()[-2:]] == ["reflect", "answer"]
    assert "stale_circuit_breaker" not in obs.reasons()
    assert "只展开了其中一部分邻居" in obs.summaries()[1]


@scenario(
    name="unsafe_scope_channels",
    scope_sources=("src-1",),
    settings={"reasoning_stale_limit": 50},
    plan_queries=("前提甲 结论丙",),
    reflects=[
        act("expand_graph", expand={"object_id": "@id:版图设计"}),
        chain("@id:前提甲"),
        act("expand_community", query="@id:版图设计"),
        dict(act("search_elements", query="版图"), sufficient=True),
        ANSWER,
    ],
)
def _expect_unsafe(obs):
    assert obs.reasons().count("source_scope_unsafe_channel") == 3
    # "sufficient" ends the loop without running the retrieval it named
    assert obs.steps("reflect")[-1]["detail"]["sufficient"] is True
    assert not [s for s in obs.steps("fallback") if s["summary"].startswith("降级查原文")]
    assert obs.result["chains"] == []
    assert not obs.port_calls("retrieval.retrieve_neighbors")
    assert not obs.port_calls("retrieval.follow_chain")


# --- add_subquery and confirmed directions ---------------------------------

DIRECTIONS = ("寄生电容", "工艺角", "版图规则", "签核流程", "数据库命令", "层定义")

#: More confirmed directions than the first round and the coverage pass can
#: run, so the uncovered-directions note stays on every turn.
MANY_DIRECTIONS = tuple(f"版图方向{index:02d}" for index in range(22))


@scenario(
    name="add_subquery_directions",
    effort="overview",
    intent_queries=(QUESTION, *DIRECTIONS),
    retrieval_overrides=lambda ctx: {
        "federated_retrieve": _fail_for(ctx, "工艺角")},
    reflects=[
        lambda ctx, prompt: sub(_pending_label(prompt, 0)),
        lambda ctx, prompt: sub(_pending_label(prompt, 0)),
    ],
)
def _expect_directions(obs):
    assert "因步骤预算不足尚未执行" in obs.summaries()[0]
    disclosure = [s for s in obs.steps("skip")
                  if s["detail"].get("reason") == "intent_coverage_incomplete"]
    assert len(disclosure) == 1
    # disclosed after the loop and before the close's answer step
    steps = obs.steps()
    assert steps.index(disclosure[0]) == len(steps) - 2
    assert steps[-1]["step_type"] == "answer"
    assert any(row.get("failed") for row in obs.result["attempted"])


def _pending_label(prompt, index):
    match = re.search(r"尚未执行: (.+?)(?: 等|。)", prompt)
    labels = re.findall(r"「([^」]+)」", match.group(1)) if match else []
    return labels[index] if len(labels) > index else "未覆盖方向"


def _fail_for(ctx, needle):
    def factory(inner):
        def federated(notebook_id, query, *args, **kwargs):
            if needle in query:
                raise RuntimeError("检索暂时失败")
            return inner(notebook_id, query, *args, **kwargs)
        return federated
    return ("wrap", factory)


@scenario(
    name="add_subquery_skips",
    settings={"reasoning_stale_limit": 50},
    intent_queries=(QUESTION, "寄生电容\n(已确认问题:版图设计要点有哪些)"),
    reflects=[
        act("add_subquery"),
        sub(QUESTION),
        sub("寄生电容"),
        sub("工艺角 与 版图"),
        ANSWER,
    ],
)
def _expect_subquery_skips(obs):
    reasons = obs.reasons()
    assert reasons.count("missing_new_sub_query") == 1
    assert reasons.count("duplicate_subquery") == 2
    assert [s for s in obs.steps("retrieve") if s["summary"].startswith("补充子查询")]


# --- search_elements, first-round handoff, stale breaker --------------------

@scenario(
    name="empty_notebook_first_round",
    notebook="empty",
    settings={"reasoning_max_element_searches": 2, "reasoning_stale_limit": 4},
    reflects=[
        act("search_elements", query="版图"),
        act("search_elements", query="版图"),
        act("exact_lookup", query="set_db"),
        ANSWER,
    ],
)
def _expect_empty(obs):
    summaries = obs.summaries()
    assert "首轮检索未命中任何证据" in summaries[0]
    assert "上一步检索未带来新证据" in summaries[1]
    # elements_searches inherited from the first-round fallback: 1 + 1 = cap
    assert len(obs.steps("fallback")) == 2
    assert obs.reasons()[:2] == ["kg_unavailable", "element_search_cap"]
    assert obs.reflect_inputs[0]["kwargs"].get("kg_actions") is False
    assert obs.reasons()[-1] == "stale_circuit_breaker"
    assert len(obs.reflect_inputs) == 3


@scenario(
    name="search_elements_until_cap",
    settings={"reasoning_max_element_searches": 2},
    reflects=[
        act("search_elements", query="寄生电容"),
        act("search_elements", query="工艺角"),
        act("search_elements", query="版图"),
        "[1]",
        ANSWER,
    ],
)
def _expect_elements(obs):
    assert len(obs.steps("fallback")) == 2
    assert obs.reasons().count("element_search_cap") == 1
    # an unparseable turn falls back to answering, and says why
    assert "fallback_reason" in obs.reflect_details()[-1]
    assert len(obs.reflect_inputs) == 4


# --- exact_lookup -----------------------------------------------------------

@scenario(
    name="exact_lookup_branches",
    effort="deep",
    settings={"graph_ppr_enabled": False, "reasoning_stale_limit": 50},
    question="set_db 在版图设计里怎么用",
    reflects=[
        act("exact_lookup", query="set_db"),
        act("exact_lookup", query="第 2.1 节"),
        act("exact_lookup", query=""),
        act("exact_lookup", query="report_timing"),
        act("exact_lookup", query="get_db"),
        act("exact_lookup", query="read_lef"),
        act("exact_lookup", query="foo_bar"),
        act("exact_lookup", query="第 2.1 节"),
        act("answer"),
        ANSWER,
    ],
)
def _expect_exact(obs):
    reasons = obs.reasons()
    assert reasons.count("duplicate_exact_lookup") == 1
    assert reasons.count("exact_term_not_identifier") == 2
    assert reasons.count("missing_exact_term") == 1
    assert reasons.count("exact_lookup_cap") == 1
    agent = [s for s in obs.steps("exact_lookup") if s["detail"].get("phase") == "reflect"]
    assert len(agent) == 3
    # repeated invalid input bumps the ledger's tries instead of a new entry
    assert "2.1" in obs.summaries()[-1]
    assert obs.steps("reflect")[-1]["detail"]["sufficient"] is False
    assert len(obs.reflect_inputs) == 9


# --- follow_chain -----------------------------------------------------------

@scenario(
    name="follow_chain_branches",
    effort="deep",
    settings={"reasoning_stale_limit": 50},
    plan_queries=("前提甲 结论丙", "桥接乙"),
    retrieval_overrides=lambda ctx: {
        "follow_chain": _fail_chain_for(ctx, "桥接乙")},
    reflects=[
        chain(""),
        chain("ko-not-a-candidate"),
        chain("@id:前提甲"),
        chain("@id:前提甲"),
        chain("@id:桥接乙"),
        chain("@id:前提甲", direction="both"),
        chain("@id:结论丙", direction="in"),
        ANSWER,
    ],
)
def _expect_follow_chain(obs):
    reasons = obs.reasons()
    for reason in ("missing_chain_start", "chain_start_not_candidate",
                   "duplicate_follow_chain", "follow_chain_cap"):
        assert reasons.count(reason) == 1, reason
    chains = obs.steps("follow_chain")
    assert chains[0]["detail"]["count"] == 1
    # the raising call fails open: recorded as an empty two-hop step
    assert chains[1]["detail"]["count"] == 0
    assert len(obs.result["chains"]) == 1


def _fail_chain_for(ctx, name):
    def factory(inner):
        def follow(notebook_id, start_object_id, *args, **kwargs):
            if start_object_id == ctx["ids"][name]:
                raise RuntimeError("图读取失败")
            return inner(notebook_id, start_object_id, *args, **kwargs)
        return follow
    return ("wrap", factory)


# --- outline ------------------------------------------------------------------

@scenario(
    name="outline_bind_kg_gap_and_nudge",
    effort="exhaustive",
    intent_queries=(QUESTION, "寄生电容", "工艺角"),
    plan_queries=("版图设计", "版图规则"),
    settings={"reasoning_max_outline_updates": 3, "reasoning_stale_limit": 50},
    reflects=[
        act("search_elements", query="版图"),
        act("search_elements", query="签核"),
        act("search_elements", query="命令"),
        lambda ctx, prompt: outline({"id": "s1", "title": "规则清单",
                                     "evidence": [ctx["ids"]["版图规则"]]}),
        act("search_elements", query="工艺"),
        lambda ctx, prompt: outline(
            {"id": "s1", "title": "规则清单", "evidence": []},
            {"id": "s2", "title": "版图要点",
             "evidence": [ctx["ids"]["版图设计"]]}),
        outline(),
        outline({"id": "s1", "title": "规则清单"}, {"id": "s2", "title": "版图要点"}),
        outline({"id": "s1", "title": "规则清单"}),
        outline({"id": "s1", "title": "规则清单"}, sufficient=True),
        ANSWER,
    ],
)
def _expect_outline(obs):
    details = obs.reflect_details()
    # at most two nudging rounds while the outline is still empty
    assert [d.get("outline_nudged") for d in details[:3]] == [True, True, None]
    assert "outline_nudged" not in details[-1]
    gap = [s for s in obs.steps("outline") if "kg_gap_candidates" in s["detail"]]
    # the second apply adds fresh weak edges on top of a still-pending batch
    assert len(gap) == 2 and gap[0]["detail"]["kg_gap_candidates"] > 6
    shown = [summary for summary in obs.summaries() if "库内支撑薄弱" in summary]
    assert len(shown) >= 2 and shown[0] != shown[1]
    assert "outline_empty" in obs.reasons()
    assert obs.reasons().count("outline_budget") == 2
    # a sufficient update_outline is applied first, then the loop ends
    assert obs.steps("reflect")[-1]["detail"]["sufficient"] is True


def _overflow_pair(sufficient_second: bool):
    return [
        lambda ctx, prompt: outline(section_with_shown("s1", "版图要点", 0, 8)(ctx, prompt)),
        lambda ctx, prompt: outline(
            section_with_shown("s1", "版图要点", 8, 9)(ctx, prompt),
            {"id": "s2", "title": "规则", "evidence": [ctx["ids"]["版图规则"]]},
            sufficient=sufficient_second),
    ]


@scenario(
    name="outline_overflow_repair_accepted",
    effort="exhaustive", plan_queries=("版图设计", "版图规则"),
    reflects=_overflow_pair(True) + [
        lambda ctx, prompt: outline(
            section_with_shown("s1", "版图要点", 8, 9,
                               remove_evidence=shown_ids(prompt)[:1])(ctx, prompt),
            {"id": "s2", "title": "规则", "evidence": []}),
        ANSWER,
    ],
)
def _expect_repair_accepted(obs):
    outlines = obs.steps("outline")
    assert outlines[1]["detail"]["overflow_evidence"]
    assert outlines[-1]["detail"]["overflow_repair"] is True
    assert outlines[-1]["detail"]["overflow_evidence"] == {}
    assert "outline_evidence_overflow_unresolved" not in obs.reasons()
    # the repair round neither shows nor consumes the weak-support hints
    assert "库内支撑薄弱" not in obs.summaries()[-1]


@scenario(
    name="outline_overflow_repair_declined_after_stale",
    effort="exhaustive", plan_queries=("版图设计", "版图规则"),
    reflects=_overflow_pair(False) + [
        act("expand_graph", expand={"object_id": ""}),
        act("expand_graph", expand={"object_id": ""}),
        act("expand_graph", expand={"object_id": ""}),
        act("search_elements", query="版图"),
        ANSWER,
    ],
)
def _expect_repair_declined(obs):
    reasons = obs.reasons()
    assert "stale_circuit_breaker" in reasons
    assert "outline_overflow_repair_declined" in reasons
    assert reasons[-1] == "outline_evidence_overflow_unresolved"


@scenario(
    name="outline_overflow_repair_structure_change",
    effort="exhaustive", plan_queries=("版图设计", "版图规则"),
    retrieval_overrides=lambda ctx: {
        "weak_support_relations": _raise(RuntimeError("图谱支撑读取失败"))},
    reflects=_overflow_pair(True) + [
        outline({"id": "s1", "title": "改过的标题"}),
        ANSWER,
    ],
)
def _expect_repair_structure(obs):
    reasons = obs.reasons()
    # an ordinary weak-support probe failure is a skip (a control error ends
    # the run instead: ``kg_gap_probe_control_error`` in the stage scenarios)
    assert "kg_gap_unavailable" in reasons
    assert obs.data["outcome"] == {"returned": True}
    assert "outline_repair_structure" in reasons
    assert reasons[-1] == "outline_evidence_overflow_unresolved"


@scenario(
    name="outline_cap_repair",
    effort="exhaustive", plan_queries=("版图设计", "版图规则"),
    settings={"reasoning_max_outline_updates": 2, "reasoning_stale_limit": 50},
    reflects=_overflow_pair(False) + [
        lambda ctx, prompt: outline(
            section_with_shown("s1", "版图要点", 8, 9,
                               remove_evidence=shown_ids(prompt)[:1])(ctx, prompt),
            {"id": "s2", "title": "规则", "evidence": []}),
        outline({"id": "s1", "title": "版图要点"}),
        ANSWER,
    ],
)
def _expect_cap_repair(obs):
    outlines = obs.steps("outline")
    assert outlines[-1]["detail"]["overflow_repair"] is True
    assert "outline_budget" in obs.reasons()


@scenario(
    name="outline_cap_repair_spent_on_rejected_structure",
    effort="exhaustive", plan_queries=("版图设计", "版图规则"),
    settings={"reasoning_max_outline_updates": 2, "reasoning_stale_limit": 50},
    reflects=_overflow_pair(False) + [
        outline({"id": "s1", "title": "改过的标题"}),
        lambda ctx, prompt: outline(
            section_with_shown("s1", "版图要点", 8, 9,
                               remove_evidence=shown_ids(prompt)[:1])(ctx, prompt),
            {"id": "s2", "title": "规则", "evidence": []}),
        ANSWER,
    ],
)
def _expect_cap_repair_spent(obs):
    # the one cap-repair chance is spent by the rejected submission: the
    # same-structure retry is an ordinary over-budget update, not a repair
    assert obs.reasons() == [
        "outline_repair_structure", "outline_budget",
        "outline_evidence_overflow_unresolved"]


@scenario(
    name="outline_overflow_on_the_last_step",
    effort="exhaustive", max_steps=2, plan_queries=("版图设计", "版图规则"),
    reflects=_overflow_pair(True) + [ANSWER],
)
def _expect_overflow_last_step(obs):
    # no step left for a repair round: the overflow is disclosed instead
    assert len(obs.steps("reflect")) == 2
    assert obs.steps("outline")[-1]["detail"]["overflow_evidence"]
    assert obs.reasons()[-1] == "outline_evidence_overflow_unresolved"


@scenario(
    name="both_end_of_run_disclosures",
    effort="exhaustive", max_steps=4,
    intent_queries=(QUESTION, *MANY_DIRECTIONS),
    reflects=_overflow_pair(True) + [ANSWER],
)
def _expect_both_disclosures(obs):
    # the budget leaves both an overflowed outline and uncovered directions:
    # both are disclosed after the loop, overflow first, then the answer step
    assert len(obs.steps("reflect")) == 2
    assert obs.reasons()[-2:] == [
        "outline_evidence_overflow_unresolved", "intent_coverage_incomplete"]
    assert [s["step_type"] for s in obs.steps()[-3:]] == ["skip", "skip", "answer"]


# --- consult_memory, zero-hit nudges, adoption -------------------------------



def _long_rationale(channel: str) -> str:
    """~150 characters: two such rows fill the consult block, a third cannot."""
    return (f"这类库上{channel}通常拿不到新证据,建议先换用其他通道再回头确认;" * 4)[:150]


@scenario(
    name="consult_memory_flow",
    effort="deep", memory=True, intent_detail=INTENT_DETAIL,
    plan_queries=("版图设计", "前提甲"),
    settings={"retrieval_experience_inject_enabled": True,
              "reasoning_max_consult_memory": 3, "reasoning_stale_limit": 50},
    experiences=(
        ("expand", "good", "expand 在这类库上通常能找到相邻概念。", 20),
        ("retrieve", "good", "retrieve 先用整题检索打底。", 19),
        ("expand_community", "good", "expand_community 适合找同类实体。", 18),
        ("exact_lookup", "bad", _long_rationale("按名称查找"), 6),
        ("follow_chain", "bad", _long_rationale("两跳推导"), 5),
        ("ppr", "bad", _long_rationale("概念漫游"), 4),
        ("search_chunks", "bad", "原文检索在这里常空手。", 3),
    ),
    profile=(("", "corpus_shape", "这个库主要是版图与签核的中文手册。"),),
    reflects=[
        act("consult_memory"),
        act("exact_lookup", query="foo_bar"),
        act("exact_lookup", query="baz_qux"),
        act("consult_memory"),
        chain("@id:版图设计", edge_type="kind_of"),
        chain("@id:版图设计", edge_type="part_of"),
        act("search_chunks", query="不存在的词甲"),
        act("search_chunks", query="不存在的词乙"),
        act("consult_memory"),
        act("consult_memory"),
        act("expand_graph", expand={"object_id": "@id:版图设计"}),
        ANSWER,
    ],
)
def _expect_consult(obs):
    reasons = obs.reasons()
    consults = obs.steps("consult_memory")
    assert [step["detail"]["entries"] for step in consults] == [2, 1]
    assert reasons.count("consult_memory_block_full") == 1
    assert reasons.count("consult_memory_cap") == 1
    details = obs.reflect_details()
    # a delivering consult turn leaves stale where it was; the next dry turn bumps it
    assert details[1]["stale"] == details[0]["stale"]
    assert details[2]["stale"] == details[1]["stale"] + 1
    assert "[Recalled search tactics]" in obs.summaries()[1]
    # the round's context blocks keep their order: profile, injected tactics,
    # recalled tactics, then the collection map with its allowance sentence
    second = obs.summaries()[1]
    marks = [second.index(head) for head in (
        "[What the agent knows about this library]",
        "[Search tactics learned from earlier runs]",
        "[Recalled search tactics]",
        "[Collections in scope]")]
    assert marks == sorted(marks)
    nudged = [s for s in obs.summaries() if "以往打法经验" in s]
    assert len({s.split("以往打法经验")[0][-40:] for s in nudged}) >= 2
    assert obs.port_calls("experiences.note_adopted")


@scenario(
    name="consult_memory_report_edges",
    entry="report", effort="deep", max_steps=4, intent_detail=INTENT_DETAIL,
    settings={"retrieval_experience_inject_enabled": True,
              "reasoning_max_consult_memory": 5, "reasoning_stale_limit": 50},
    experiences=(
        ("expand", "good", "expand 在这类库上通常能找到相邻概念。", 20),
        ("retrieve", "good", "retrieve 先用整题检索打底。", 19),
    ),
    experience_overrides=lambda ctx: {
        "version_signal": _fault_switch(ctx, "version_signal")},
    reflects=[
        act("consult_memory"),
        Effect(lambda ctx: ctx.update(fault=True), act("consult_memory")),
        Effect(lambda ctx: ctx.update(fault=False),
               act("search_elements", query="版图")),
        act("consult_memory"),
    ],
)
def _expect_consult_edges(obs):
    reasons = obs.reasons()
    assert reasons[:2] == ["consult_memory_nothing_new", "consult_memory_unavailable"]
    assert "consult_memory_last_turn" in reasons


def _fault_switch(ctx, name):
    def factory(inner):
        def call(*args, **kwargs):
            if ctx["fault"]:
                raise RuntimeError("经验库读取失败")
            return inner(*args, **kwargs)
        return call
    return ("wrap", factory)


_CROWDING_BLOCK = "工艺关键词" * 90


@scenario(
    name="consult_memory_overlay_once",
    effort="deep", memory=True, intent_detail=INTENT_DETAIL,
    settings={"retrieval_experience_inject_enabled": True,
              "reasoning_stale_limit": 50},
    profile=(
        ("", "corpus_shape", _CROWDING_BLOCK),
        ("", "key_entities", _CROWDING_BLOCK),
        ("", "corpus_gaps", _CROWDING_BLOCK),
        (OWNER, "retrieval_notes", "这位成员常问时序收敛细节"),
    ),
    reflects=[act("consult_memory"), act("consult_memory"), ANSWER],
)
def _expect_overlay_once(obs):
    # the owner's note was crowded out of the profile block, so the first
    # consult delivers it; the second has nothing new to say
    consults = obs.steps("consult_memory")
    assert len(consults) == 1 and consults[0]["detail"]["entries"] == 0
    assert "时序收敛" in obs.summaries()[1]
    assert obs.reasons() == ["consult_memory_nothing_new"]


@scenario(
    name="consult_memory_disabled_by_caller",
    effort="deep", memory=True, intent_detail=INTENT_DETAIL,
    settings={"retrieval_experience_inject_enabled": True},
    experiences=(("ppr", "bad", "ppr 在这类库上很少有收获。", 3),),
    allow={"allow_consult_memory": False},
    reflects=[act("consult_memory"), ANSWER],
)
def _expect_consult_disabled(obs):
    assert obs.reasons() == ["consult_memory_disabled"]


# --- in-document navigation: browse_sections / read_section ----------------

def browse(source, under="", query=""):
    return act("browse_sections", browse_sections={
        "source": source, "under": under, "query": query})


def read_section(section, source=""):
    return act("read_section", read_section={"source": source, "section": section})


@scenario(
    name="section_navigation_branches",
    notebook="sections", effort=None,  # the raw step budget, not standard's 8
    settings={"reasoning_max_section_browses": 2, "reasoning_max_section_reads": 2,
              "reasoning_stale_limit": 50, "reasoning_max_steps": 20},
    reflects=[
        act("enumerate_elements", enumerate={"collection": "sources"}),
        browse("互连规范"),
        browse("互连规范"),
        browse("互连规范", under="附录甲"),
        browse("互连规范", under="9"),
        browse("不存在的文档"),
        browse("空白文档"),
        read_section("s99"),
        read_section("Notes", source="互连规范"),
        read_section("5.7.3", source="互连规范"),
        read_section("5.7.3 Standard Package", source="互连规范"),
        read_section("s2"),
        # spent actions leave the enum, so a model can no longer pick them;
        # the caps are the executors' own defence in depth
        RawDecision("read_section"),
        browse("互连规范", under="5.7"),
        RawDecision("browse_sections"),
        ANSWER,
    ],
)
def _expect_section_navigation(obs):
    reasons = [r for r in obs.reasons() if r.startswith("section_")]
    assert reasons == [
        "section_under_unsupported", "section_under_empty",
        "section_source_unresolved", "section_outline_empty",
        "section_not_surfaced", "section_ambiguous", "section_read_repeat",
        "section_read_cap", "section_browse_cap"]
    browses = obs.steps("browse_sections")
    # the second, identical browse puts the cached listing back (zero I/O)
    assert [(s["detail"]["under"], s["detail"].get("repeat", False))
            for s in browses] == [("", False), ("", True), ("5.7", False)]
    assert all(s["detail"]["listed"] > 0 and "result_ids" not in s["detail"]
               for s in browses)
    reads = obs.steps("read_section")
    assert [s["detail"]["section"] for s in reads] == [
        "5.7.3 Standard Package", "5.7 Channel"]
    # a leaf (the Notes after it) does not end 5.7.3: it is part of its range
    assert reads[0]["detail"]["result_ids"] == ["c-s3", "c-s4"]
    # 5.7's range is 5.7, its virtual 5.7.2 / 5.7.2.1 and 5.7.3 (already read)
    assert reads[1]["detail"]["result_ids"] == ["c-s1", "c-s2"]
    # the outline is read once per source and run, whatever the turn count
    assert len(obs.port_calls("retrieval.section_outline")) == 2
    summaries = obs.summaries()
    # the listing reaches the next turn, and the next listing of the same
    # document replaces it (one listing block, the latest)
    assert "（章节目录:《互连规范》,列出" in summaries[2]
    assert summaries[-1].count("（章节目录:") == 1
    assert "（章节目录:《互连规范》 under 5.7," in summaries[-1]
    assert "已浏览《互连规范》" in summaries[-1] and "已读《互连规范》" in summaries[-1]
    assert "未执行:查看章节目录" in summaries[-1]
    # after a new listing the turn is told to read it, not to give up; a
    # replayed listing is no new information and gets the ordinary note
    assert "上一步列出了章节目录" in summaries[2]
    assert "上一步检索未带来新证据" not in summaries[2]
    assert "上一步检索未带来新证据" in summaries[3]
    # a new listing holds stale; the replay after it does not
    details = obs.reflect_details()
    assert details[2]["no_progress"] is True and details[2]["stale"] == 0
    assert details[3]["stale"] == 1
    # with reads spent, the hint text and the listing stop pointing at read_section
    assert "按 [sN] 读取原文" not in summaries[-1]


def _raise_section_io(*args, **kwargs):
    raise RuntimeError("大纲读取失败")


@scenario(
    name="section_navigation_failures",
    notebook="sections",
    retrieval_overrides=lambda ctx: {"section_outline": _raise_section_io},
    reflects=[
        act("enumerate_elements", enumerate={"collection": "sources"}),
        browse("互连规范"),
        read_section("6", source="互连规范"),
        ANSWER,
    ],
)
def _expect_section_navigation_failures(obs):
    # one failed read never voids the run: both turn into skips, the roster
    # listed before them is kept, and the run still answers
    assert obs.data["outcome"].get("returned") is True
    failed = [s for s in obs.steps("skip")
              if s["detail"].get("reason", "").endswith("_failed")]
    assert [(s["detail"]["reason"], s["detail"]["error"]) for s in failed] == [
        ("section_browse_failed", "RuntimeError"),
        ("section_read_failed", "RuntimeError")]
    assert all("result_ids" not in s["detail"] for s in failed)
    assert obs.steps("enumerate") and obs.steps()[-1]["step_type"] == "answer"
    assert "未执行:查看章节目录:读取时出错(RuntimeError)" in obs.summaries()[-1]


@scenario(
    name="section_navigation_closed",
    notebook="sections",
    settings={"reasoning_section_nav_enabled": False},
    reflects=[RawDecision("browse_sections"), RawDecision("read_section"), ANSWER],
)
def _expect_section_navigation_closed(obs):
    assert [r for r in obs.reasons() if r.startswith("section_")] == [
        "section_nav_disabled", "section_nav_disabled"]
    assert not obs.steps("browse_sections") and not obs.steps("read_section")
    # zero I/O: no outline, similarity or section read reaches the port
    assert not obs.port_calls("retrieval.section_")


# --- every block of the candidate summary, in one run ----------------------

@scenario(
    name="every_summary_block_in_one_run",
    entry="ask", effort="exhaustive", intent_detail=INTENT_DETAIL,
    intent_queries=(QUESTION, *MANY_DIRECTIONS),
    settings={"retrieval_experience_inject_enabled": True,
              "reasoning_max_steps": 16, "reasoning_stale_limit": 50,
              "reasoning_max_consult_memory": 3},
    experiences=(
        ("expand", "good", "expand 在这类库上通常能找到相邻概念。", 20),
        ("retrieve", "good", "retrieve 先用整题检索打底。", 19),
        ("expand_community", "good", "expand_community 适合找同类实体。", 18),
        ("follow_chain", "good", "follow_chain 能补上两跳关系。", 7),
        ("exact_lookup", "bad", "按名称查找在这类库上常常空手。", 6),
    ),
    profile=(("", "corpus_shape", "这个库主要是版图与签核的中文手册。"),),
    reflects=[
        act("enumerate_elements", enumerate={"collection": "sources"}),
        act("consult_memory"),
        browse("版图手册"),
        outline({"id": "s1", "title": "版图要点", "evidence": []}),
        act("exact_lookup", query="foo_bar"),
        act("exact_lookup", query="baz_qux"),
        act("search_papers", search_papers={"query": "版图寄生提取"}),
        ANSWER,
    ],
)
def _expect_every_block(obs):
    # the turn after the second dry exact lookup carries every block that can
    # share a turn (the outline nudge only shows while there is no outline)
    blocks = [_summary_blocks(text) for text in obs.summaries()]
    assert [name for name, _ in blocks[6]] == [
        "evidence", "progress_note", "ledger", "uncovered", "zero_hit_nudge",
        "enumeration", "outline", "profile", "experience", "consult",
        "section_listing", "collection_map", "plugin_nudge"]
    assert [name for name, _ in blocks[1]][:8] == [
        "evidence", "ledger", "uncovered", "enumeration", "outline_nudge",
        "profile", "experience", "collection_map"]
    assert {"external", "section_listing"} <= {name for name, _ in blocks[7]}
    assert obs.steps("plugin_action")


# --- limits=None: the raw limits, not the tier fallback ---------------------

def _ascending_elements(count):
    """``retrieve_elements`` answering with ``count`` elements whose scores rise
    in arrival order, so a score ranking would reverse them."""
    from app.domain.retrieval import RetrievedElement

    def retrieve(notebook_id, query, *args, **kwargs):
        return [
            RetrievedElement(
                element_id=f"el-asc-{index}", source_id="src-1",
                source_title="版图手册", location_label=f"第 {index} 页",
                element_type="table", text=f"递增要素{index} 的表格内容",
                score=0.1 * (index + 1))
            for index in range(count)
        ]
    return retrieve


@scenario(
    name="limits_none_reads_the_raw_limits",
    effort=None, memory=True, intent_detail=INTENT_DETAIL,
    settings={"retrieval_experience_inject_enabled": True,
              "reasoning_stale_limit": 50, "retrieval_top_n": 8},
    # the same action learned in two situations: the run's own (no tier, so the
    # contract's effort) and the default tier's
    experiences=(
        ("exact_lookup", "bad", "按名称查找在这类库上常常空手。", 5),
        ("exact_lookup", "bad", "标准档里按名称查找没有收获。", 5, "standard"),
    ),
    retrieval_overrides=lambda ctx: {"retrieve_elements": _ascending_elements(8)},
    reflects=[
        act("exact_lookup", query="foo_bar"),
        act("exact_lookup", query="baz_qux"),
        act("search_elements", query="版图"),
        ANSWER,
    ],
)
def _expect_limits_none(obs):
    # a run without limits (knowhow's shape) keeps every raw-limits reading:
    # the summary shows all elements in arrival order, not the default tier's
    # ranked six ...
    after_search = obs.summaries()[3]
    shown = re.findall(r"递增要素(\d)", after_search)
    assert shown == [str(index) for index in range(8)]
    # ... the zero-hit nudge matches the experience recorded for this run's own
    # situation (no tier effort, so the contract's) ...
    nudged = [text for text in obs.summaries() if "以往打法经验" in text]
    assert nudged and "按名称查找在这类库上常常空手" in nudged[0]
    assert not any("标准档里按名称查找没有收获" in text for text in obs.summaries())
    # ... and the evidence budget is the deployment's, not the default tier's
    assert obs.steps("answer")[-1]["detail"]["top_n"] == 8


# --- cancellation -----------------------------------------------------------

@scenario(
    name="cancelled_mid_loop",
    reflects=[
        sub("寄生电容"),
        Effect(lambda ctx: ctx["cancel"].set(), sub("工艺角")),
        ANSWER,
    ],
)
def _expect_cancel(obs):
    assert obs.raised.startswith("AskCancelled")
    assert obs.result is None
    assert obs.steps()[-1]["step_type"] == "retrieve"
    assert len(obs.data["reflect_inputs"]) == 2


SCENARIO_IDS = [item.name for item in SCENARIOS]

#: The blocks of a reflect turn's candidate summary, in the order the loop
#: assembles them (``_reflect_round_ledger_notes``, then
#: ``_reflect_round_context_blocks``, then the plugin context), each named by
#: the text its rendering starts with. The outline note and the outline nudge
#: share one slot and never appear together.
SUMMARY_BLOCKS = (
    ("evidence", ("- [",)),
    ("progress_note", ("（系统提示:首轮检索未命中任何证据", "（系统提示:上一步检索未带来新证据",
                       "（系统提示:上一步列出了章节目录")),
    ("ledger", ("（已展开过的节点", "（以下节点的关系数超过", "（已执行过的子查询及各自新增证据数",
                "（已按名称精确查找过及各自结果", "已按篇读取过原文的文档",
                "本次没有取到原文的文档", "（章节导航记录")),
    ("uncovered", ("（以下已确认的检索方向因步骤预算不足尚未执行",)),
    ("zero_hit_nudge", ("」这类动作在当前场景已连续",)),
    ("enumeration", ("（本轮已枚举的清单",)),
    ("outline", ("（本轮大纲便签",)),
    ("outline_nudge", ("（本轮尚未建立大纲",)),
    ("profile", ("[What the agent knows about this library]",)),
    ("experience", ("[Search tactics learned from earlier runs]",)),
    ("consult", ("[Recalled search tactics]",)),
    ("external", ("[External evidence]",)),
    ("section_listing", ("（章节目录:",)),
    ("collection_map", ("[Collections in scope]",)),
    ("plugin_nudge", ("（系统提示:库内检索已无新证据",)),
)
#: Adjacent blocks that never share a turn, so their order is unobservable.
EXCLUSIVE_BLOCKS = frozenset({("outline", "outline_nudge")})


def _summary_blocks(text: str) -> list:
    """``[(block, start), ...]`` for the blocks present, in text order."""
    found = []
    for name, marks in SUMMARY_BLOCKS:
        starts = [text.find(mark) for mark in marks if mark in text]
        if starts:
            found.append((name, min(starts)))
    return sorted(found, key=lambda item: item[1])


def _assert_run_shape(observed: dict) -> None:
    """What every scenario's run must look like, whatever it was driving at.

    * each reflect turn's candidate summary keeps its blocks in assembly order;
    * a returned run closes with the answer step, and the two end-of-run
      disclosures sit between the last reflect turn and that answer step, the
      outline one first.
    """
    order = [name for name, _ in SUMMARY_BLOCKS]
    for entry in observed["reflect_inputs"]:
        names = [name for name, _ in _summary_blocks(entry["candidates_summary"])]
        assert names == sorted(names, key=order.index), names
    trace = observed["trace"]
    if not observed["outcome"].get("returned"):
        return
    assert trace[-1]["step_type"] == "answer"
    last_reflect = max(
        (index for index, step in enumerate(trace) if step["step_type"] == "reflect"),
        default=-1)
    closing = [
        (index, step["detail"].get("reason")) for index, step in enumerate(trace)
        if step["step_type"] == "skip" and step["detail"].get("reason") in (
            "outline_evidence_overflow_unresolved", "intent_coverage_incomplete")]
    assert all(last_reflect < index < len(trace) - 1 for index, _ in closing), closing
    reasons = [reason for _, reason in closing]
    assert reasons == sorted(reasons, key=[
        "outline_evidence_overflow_unresolved", "intent_coverage_incomplete"].index)



#: Every skip reason the reflect loop itself can record, minus the ones only a
#: wall clock or a peer-mode run reaches. The scenarios must reach all of them —
#: a branch no scenario takes is a branch no assertion here vouches for.
LOOP_SKIP_REASONS = frozenset({
    "source_scope_unsafe_channel", "empty_or_visited", "missing_new_sub_query",
    "duplicate_subquery", "element_search_cap", "enumeration_disabled",
    "consult_memory_disabled", "consult_memory_cap", "consult_memory_last_turn",
    "consult_memory_nothing_new", "consult_memory_block_full",
    "consult_memory_unavailable", "exact_lookup_disabled", "missing_exact_term",
    "exact_term_not_identifier", "duplicate_exact_lookup", "exact_lookup_cap",
    "missing_chain_start", "chain_start_not_candidate", "duplicate_follow_chain",
    "follow_chain_cap", "community_expansion_disabled", "stale_circuit_breaker",
    "outline_overflow_repair_declined", "outline_repair_structure",
    "outline_budget", "outline_empty", "kg_gap_unavailable",
    "outline_evidence_overflow_unresolved", "intent_coverage_incomplete",
    "section_nav_disabled", "section_browse_cap", "section_source_unresolved",
    "section_outline_empty", "section_under_unsupported", "section_under_empty",
    "section_read_cap", "section_not_surfaced", "section_ambiguous",
    "section_read_repeat", "section_browse_failed", "section_read_failed",
})


# ------------------------------------------------------------------- tests


CHECKOUT_LOGS = Path(__file__).resolve().parents[2] / ".local" / "logs"


def _checkout_log_files() -> dict:
    """Every file under the checkout's log root with its size and mtime: an
    append to an existing day file must show up, not only a new file."""
    if not CHECKOUT_LOGS.exists():
        return {}
    return {
        path: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in CHECKOUT_LOGS.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize("case", SCENARIOS, ids=SCENARIO_IDS)
def test_reflect_loop_scenario(case, tmp_path, monkeypatch):
    before = _checkout_log_files()
    observed = observe(case, tmp_path, monkeypatch)
    # every log the run writes stays under this test's tmp_path
    assert _checkout_log_files() == before
    assert any((tmp_path / "logs").rglob("*.jsonl"))
    _assert_run_shape(observed)
    case.expect(Observed(observed))


#: Deployment variables a developer may have exported (one of them in lower
#: case: the settings lookup is case-insensitive).
_LEAKY_EXPORTS = {
    "REASONING_STALE_LIMIT": "1",
    "RELATION_RETRIEVAL_ENABLED": "true",
    "REASONING_MAX_STEPS": "3",
    "GRAPH_PPR_ENABLED": "false",
    "reasoning_max_exact_lookups": "1",
}


def test_scenarios_reach_every_loop_branch_whatever_the_shell_exports(
    tmp_path, monkeypatch,
):
    """Every scenario keeps its behaviour with deployment variables exported in
    the shell (each run builds its settings from a scrubbed environment), and
    the scenarios are not vacuous: together they take every skip branch of the
    loop, the step kinds only the loop's dispatch produces, both ways a run can
    raise, and every pair of neighbouring summary blocks shares at least one
    turn (so ``_assert_run_shape`` can see either one move past the other).
    The four entry shapes are checked on the scenario declarations
    (``case.entry``), not observed.

    Since the split oracle retired, this compares behaviour expectations under
    exported variables, not the byte-level observation: the expected cost of
    retiring it."""
    for key, value in _LEAKY_EXPORTS.items():
        monkeypatch.setenv(key, value)
    reasons, kinds, raised, neighbours = set(), set(), set(), set()
    for case in SCENARIOS:
        scenario_dir = tmp_path / case.name
        scenario_dir.mkdir()
        with pytest.MonkeyPatch.context() as scoped:
            observed = observe(case, scenario_dir, scoped)
        _assert_run_shape(observed)
        case.expect(Observed(observed))
        for entry in observed["reflect_inputs"]:
            names = {name for name, _ in _summary_blocks(entry["candidates_summary"])}
            neighbours.update(
                (a, b) for (a, _), (b, _) in zip(SUMMARY_BLOCKS, SUMMARY_BLOCKS[1:])
                if a in names and b in names)
        for step in observed["trace"]:
            kinds.add(step["step_type"])
            if step["step_type"] == "skip":
                reasons.add(step["detail"].get("reason"))
        raised.add(observed["outcome"].get("raised", "").split(":")[0])
    assert LOOP_SKIP_REASONS <= reasons, sorted(LOOP_SKIP_REASONS - reasons)
    assert {"expand", "follow_chain", "outline", "consult_memory",
            "plugin_action", "enumerate", "read_document", "exact_lookup",
            "expand_community", "browse_sections", "read_section"} <= kinds
    assert {"AskCancelled", "RuntimeError"} <= raised
    assert {case.entry for case in SCENARIOS} == {"run", "report", "ask", "knowhow"}
    pairs = {(a, b) for (a, _), (b, _) in zip(SUMMARY_BLOCKS, SUMMARY_BLOCKS[1:])}
    assert pairs - EXCLUSIVE_BLOCKS <= neighbours, sorted(pairs - EXCLUSIVE_BLOCKS - neighbours)
