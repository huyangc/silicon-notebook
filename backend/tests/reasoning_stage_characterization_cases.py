"""Behaviour rig for ``AskService._run_reasoning_stage`` (the reasoning Ask orchestrator).

**两端吃同一份世界、同一套观测、同一组场景**:``tests/test_reasoning_stage_characterization.py``
(SQLite)与 ``tests/postgres/test_reasoning_stage_characterization_pg.py``(PostgreSQL)各自只写
一个 ``Backend`` 适配器(有哪些表、缓存怎么清)。不是测试模块(没有 ``test_`` 前缀),pytest 不会
收集它。

每个场景经生产入口(``AskService.ask`` / ``AskService.ask_evidence``,即 ``_engine_scope`` 装好
检索天花板与 retrieval run 之后)跑一次真实的逐步推理 Ask:真实仓库、真实检索与合成,只有模型是
脚本化的确定性替身。``observe`` 录下断言要读的东西:对服务自身协作方法与 ``execute_*_stage`` /
``kg_in_scope_for`` / 结构化枚举 / ``_egress_question`` 的调用(规范化后的参数与返回值)、模型收到
的提示词、查询 embedding、流式推送的每一步、本线程日志、表的增删行数、两个请求级 ContextVar 在
调用之后的值,以及返回值或异常。``Scenario.expect`` 断言用户与下游可见的事实——走了哪条短路、
``llm_mode`` / ``kg_required`` / 完整性披露、轨迹的步骤序列与流式推送、记忆那一步、模型错误、站外
补充、答案与会话是否落库、取消与异常的类型。

这套世界与场景最初是 ``_run_reasoning_stage`` 零行为变化拆分的表征:拆分前在两端采集的逐字节快照
(另含每次取消检查、ContextVar 的 set/reset、事件日志、本线程的每条 SQL 与整库逐行差分)拆分后不变,
随后按 AGENTS.md「不做只为重构服务的金标快照」退役,只留本行为层。
"""
from __future__ import annotations

import collections
import dataclasses
import enum
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from app.models.ask import (
    AskIntentConfirmation,
    AskRequest,
    QueryIntentAnswer,
    QueryIntentContract,
    QueryIntentTopic,
)
from app.models.notebooks import NotebookCreate
from app.repositories.ports import UploadedSourceFile
from app.services.embedding import FakeEmbedder
from app.services.model_registry import WORKLOADS
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client

EMBED_DIM = 16
ASKED_AT = "2026-10-21T08:00:00+08:00"

# --------------------------------------------------------------------- canon


class Canon:
    """A plain JSON-shaped projection of a recorded value (dataclasses and
    pydantic models become ``{"dc:<Type>": ...}`` / ``{"pd:<Type>": ...}``)."""

    def __call__(self, value: Any, _depth: int = 0) -> Any:
        if _depth > 24:
            return f"<deep {type(value).__name__}>"
        depth = _depth + 1
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, enum.Enum):
            return self(value.value, depth)
        if isinstance(value, threading.Event):
            return {"event": value.is_set()}
        if isinstance(value, dict):
            return {key if isinstance(key, str) else repr(key): self(item, depth)
                    for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self(v, depth) for v in value]
        if isinstance(value, (set, frozenset)):
            return sorted((self(v, depth) for v in value), key=_dumps)
        if type(value).__name__ == "Settings":
            return "<Settings>"  # never dump configuration (it can carry secrets)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {"dc:" + type(value).__name__: self({
                field.name: getattr(value, field.name)
                for field in dataclasses.fields(value)
            }, depth)}
        if hasattr(value, "model_dump"):
            return {"pd:" + type(value).__name__: self(value.model_dump(mode="python"), depth)}
        if callable(value):
            return f"<callable {getattr(value, '__name__', type(value).__name__)}>"
        return f"<{type(value).__name__}>"


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


# ------------------------------------------------------------------ recorder


class Recorder:
    """One scenario's ordered timeline; ``active`` gates every channel."""

    def __init__(self, canon: Canon) -> None:
        self.canon = canon
        self.active = False
        self.thread = threading.get_ident()
        self.timeline: list[dict] = []
        self.worker: list[dict] = []
        self._lock = threading.Lock()

    def on_thread(self) -> bool:
        return self.active and threading.get_ident() == self.thread

    def note(self, entry: dict) -> dict:
        if not self.active:
            return entry
        if threading.get_ident() == self.thread:
            self.timeline.append(entry)
        else:
            with self._lock:
                self.worker.append(entry)
        return entry


class Hooks:
    """Per-scenario interception keyed ``"<label>.<name>"``: ``before``/``after``
    side effects and ``replace`` (receives the real callable)."""

    def __init__(self) -> None:
        self.before: dict[str, Callable] = {}
        self.after: dict[str, Callable] = {}
        self.replace: dict[str, Callable] = {}


def _recorded(label: str, name: str, attr: Callable, recorder: Recorder,
              hooks: Hooks, *, args_too: bool = True) -> Callable:
    key = f"{label}.{name}"
    canon = recorder.canon

    def call(*args: Any, **kwargs: Any) -> Any:
        if not recorder.active:
            return attr(*args, **kwargs)
        entry = {"call": key}
        if args_too:
            entry.update(args=canon(args), kwargs=canon(kwargs))
        recorder.note(entry)
        if key in hooks.before:
            hooks.before[key](*args, **kwargs)
        try:
            if key in hooks.replace:
                result = hooks.replace[key](attr, *args, **kwargs)
            else:
                result = attr(*args, **kwargs)
        except BaseException as exc:
            entry["raised"] = f"{type(exc).__name__}: {canon(str(exc))}"
            raise
        if key in hooks.after:
            hooks.after[key](result, *args, **kwargs)
        entry["ret"] = canon(result)
        return result

    call.__name__ = name
    return call


# -------------------------------------------------------------------- models


class ScriptedChat:
    """One workload's chat client; every call recorded, replies scripted."""

    configured = True
    model = "fake-reasoning-model"

    def __init__(self, workload: str, models: "ScriptedModels") -> None:
        self.workload, self._models = workload, models

    def chat_json(self, messages, schema_hint=None, **kwargs):
        return self._models.answer(self.workload, messages, schema_hint, kwargs)


_KEY = re.compile(r"\[(k\d+)\]")


class ScriptedModels:
    """The deterministic model: replies by the schema a prompt asks for."""

    def __init__(self, recorder: Recorder, script: "Script") -> None:
        self.recorder = recorder
        self.script = script
        self.reflects = list(script.reflects)
        self.calls: list[dict] = []

    def answer(self, workload, messages, schema_hint, kwargs):
        hint = str(schema_hint or "")
        prompt = "\n".join(str(m.get("content", "")) for m in messages)
        entry = {"model": workload, "messages": self.recorder.canon(messages)}
        self.recorder.note(entry)
        failure = self.script.failures.get(workload)
        if failure is not None:
            entry["raised"] = type(failure).__name__
            raise failure
        return self._reply(workload, prompt, hint)

    def _reply(self, workload: str, prompt: str, hint: str) -> str:
        script = self.script
        if '"operation"' in hint:
            return json.dumps(script.sheet_plan, ensure_ascii=False)
        if "source_queries" in hint:
            return json.dumps({"source_queries": {"corp_source": [script.plan_query]}},
                              ensure_ascii=False)
        if "sub_queries" in hint:
            return json.dumps({"sub_queries": [{"query": script.plan_query}]},
                              ensure_ascii=False)
        if "next_action" in hint:
            item = self.reflects.pop(0) if self.reflects else {
                "next_action": "answer", "sufficient": True, "reason": "证据够了"}
            return json.dumps(item, ensure_ascii=False)
        if '"is_paper"' in hint:  # ingestion's paper-metadata probe (world seeding)
            return json.dumps({"is_paper": False})
        if '"kb_says"' in hint:
            return json.dumps({"text": "站外资料补充了寄生提取的做法 [X1]。", "conflicts": [
                {"kb_says": "库内要求先提取寄生", "external_says": "站外建议先做布线",
                 "note": "顺序不同"}]}, ensure_ascii=False)
        if '"relevant"' in hint:
            return json.dumps({"relevant": []})
        if '"answer"' in hint:
            keys = list(dict.fromkeys(_KEY.findall(prompt)))[:3]
            cited = "".join(f"[{key}]" for key in keys)
            return json.dumps({
                "conclusion": "版图设计需要兼顾寄生与工艺角。",
                "answer": f"版图设计需要兼顾寄生与工艺角{cited}。",
                "grounded": bool(keys),
                "anchors": [],
                "summary": "站外摘要补充。",
                "conflicts": [],
            }, ensure_ascii=False)
        return json.dumps({}, ensure_ascii=False)


@dataclasses.dataclass(frozen=True)
class Script:
    plan_query: str = "版图设计要点"
    reflects: tuple = ()
    failures: dict = dataclasses.field(default_factory=dict)
    sheet_plan: dict = dataclasses.field(default_factory=lambda: {"operation": "profile"})


class RecordingEmbedder:
    configured = True

    def __init__(self, recorder: Recorder) -> None:
        self._inner = FakeEmbedder(dim=EMBED_DIM)
        self.dim = EMBED_DIM
        self.recorder = recorder

    def embed_query(self, text):
        self.recorder.note({"embed_query": self.recorder.canon(text)})
        return self._inner.embed_query(text)

    def embed_texts(self, texts):
        self.recorder.note({"embed_texts": self.recorder.canon(list(texts))})
        return self._inner.embed_texts(texts)


# ----------------------------------------------------------------- the world

LAYOUT_MD = """# 版图设计手册

## 寄生电容

版图设计要兼顾寄生电容,长走线会显著增加耦合电容,关键网络要做屏蔽。

## 工艺角

签核前必须在全部工艺角下复核时序,慢角决定建立时间余量。

## 分层布线

常见做法是分层布线,高层金属走电源与时钟,低层金属走信号。
"""

SIGNOFF_MD = """# 签核指南

## 时序签核

report_timing 输出关键路径的时序报告,是签核阶段的主要依据。

## 物理验证

DRC 与 LVS 是物理验证的两道关口,任何一项违例都不能流片。
"""

KNOWHOW_COLUMNS = [{"name": "方法", "role": "anchor"}, {"name": "说明", "role": "attribute"}]
KNOWHOW_ROWS = [["屏蔽走线", "关键网络两侧加地线屏蔽"],
                ["插入缓冲", "长线中段插入缓冲器降低延迟"],
                ["降低负载", "减少扇出以改善转换时间"]]


def _node(local_id, name, object_type="concept", evidence=()):
    return {"local_id": local_id, "object_type": object_type,
            "payload": {"name": name, "section_path": "1"}, "evidence": list(evidence)}


def _edge(src, tgt, edge_type):
    return {"source_local_id": src, "target_local_id": tgt, "edge_type": edge_type,
            "evidence": []}


@dataclasses.dataclass
class Rig:
    repo: Any
    backend: "Backend"
    recorder: Recorder
    hooks: Hooks
    models: ScriptedModels
    tmp_path: Path
    notebook_id: str = ""
    user_id: str = ""
    sources: dict = dataclasses.field(default_factory=dict)
    cancel: Any = None

    @property
    def service(self) -> Any:
        return self.repo._runtime.ask_component

    def sql(self, statement: str, params: tuple = ()) -> None:
        statement = statement.replace("?", self.backend.placeholder)
        with self.repo._runtime.database.write() as db:
            db.execute(statement, params)

    def query(self, statement: str, params: tuple = ()) -> list[dict]:
        statement = statement.replace("?", self.backend.placeholder)
        with self.repo._runtime.database.connect() as db:
            return [dict(r) for r in db.execute(statement, params).fetchall()]

    def upload(self, name: str, content: bytes, content_type: str) -> str:
        source = self.repo.upload_sources(self.notebook_id, [UploadedSourceFile(
            file_name=name, content_type=content_type, content=content,
        )], scheduler=lambda _source_id: None)[0]
        self.repo.process_source(source.id)
        self.sources[name] = source.id
        return source.id

    def elements(self, source_id: str) -> list[dict]:
        return self.query("SELECT id, text FROM source_elements WHERE source_id=? "
                          "ORDER BY id", (source_id,))


def _notebook(rig: Rig, name: str = "版图") -> None:
    rig.notebook_id = rig.repo._runtime.catalog.create_notebook(NotebookCreate(name=name)).id
    rig.user_id = rig.repo._runtime._current_user_id()


def empty_world(rig: Rig) -> None:
    _notebook(rig, "空库")


def documents_world(rig: Rig) -> None:
    """Two parsed markdown documents (chunks, elements, embeddings), no graph."""
    _notebook(rig)
    rig.upload("layout.md", LAYOUT_MD.encode(), "text/markdown")
    rig.upload("signoff.md", SIGNOFF_MD.encode(), "text/markdown")


def library_world(rig: Rig) -> None:
    """The documents plus a small graph whose objects cite their passages."""
    documents_world(rig)
    layout = rig.sources["layout.md"]
    rows = rig.elements(layout)
    by_text = {row["text"]: row["id"] for row in rows}

    def cite(fragment: str) -> list[dict]:
        element = next(eid for text, eid in by_text.items() if fragment in text)
        return [{"source_id": layout, "source_title": "版图设计手册",
                 "element_id": element, "element_type": "paragraph",
                 "location_label": "p1", "quoted_span": fragment, "confidence": 1.0}]

    nodes = [
        _node("A", "版图设计", evidence=cite("版图设计要兼顾")),
        _node("B", "寄生电容", evidence=cite("耦合电容")),
        _node("C", "工艺角", evidence=cite("工艺角下复核")),
        _node("D", "分层布线", "procedure", evidence=cite("分层布线")),
        _node("E", "慢角决定建立时间余量", "claim", evidence=cite("慢角")),
    ]
    edges = [_edge("A", "B", "related_to"), _edge("A", "C", "part_of"),
             _edge("D", "A", "uses"), _edge("E", "C", "mentions")]
    lifecycle = rig.repo._runtime.knowledge_lifecycle
    lifecycle.store_kg(rig.notebook_id, layout, nodes, edges)
    lifecycle.rebuild_unified_kg(rig.notebook_id)


def memory_world(rig: Rig) -> None:
    """The library plus one confirmed private memory of the asker."""
    library_world(rig)
    service = rig.repo._runtime.memory_service
    scheduler = service.embedding_scheduler
    service.embedding_scheduler = lambda embed, job: embed(job)  # embed inline
    try:
        memory = service.create_candidate(
            rig.notebook_id, rig.user_id, None, "req-1", "版图评审结论",
            "上次评审确认:版图设计必须先做寄生提取再签核。", [], "task")
        service.confirm(memory.id, rig.user_id, {"extract_kg": False})
    finally:
        service.embedding_scheduler = scheduler


def knowhow_world(rig: Rig) -> None:
    """The library plus one Knowhow table of three methods."""
    library_world(rig)
    _knowhow_table(rig)


def knowhow_only_world(rig: Rig) -> None:
    """A Knowhow table and nothing else to retrieve."""
    _notebook(rig, "方法库")
    _knowhow_table(rig)


def _knowhow_table(rig: Rig) -> None:
    rig.repo._runtime.knowhow_store.create_knowhow_table_with_rows(
        rig.notebook_id, "时序优化方法", "", KNOWHOW_COLUMNS, KNOWHOW_ROWS,
        created_by=rig.user_id, actor="tester")


def workbook_world(rig: Rig) -> None:
    """The library plus one uploaded spreadsheet (the workbook lane)."""
    from openpyxl import Workbook

    library_world(rig)
    path = rig.tmp_path / "timing.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Paths"
    sheet.append(["Path", "Slack"])
    for row in (("clk_a", 12), ("clk_b", -3), ("clk_c", 7)):
        sheet.append(list(row))
    workbook.save(path)
    rig.upload("timing.xlsx", path.read_bytes(),
               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# --------------------------------------------------------------- requests

QUESTION = "版图设计要点有哪些"


def plain(question: str = QUESTION, **fields) -> Callable[[Rig], AskRequest]:
    def build(rig: Rig) -> AskRequest:
        return AskRequest(question=question, mode="reasoning", asked_at=ASKED_AT, **fields)
    return build


def confirmed(question: str = QUESTION, *, topics: tuple = ("寄生电容如何控制",),
              clarified: bool = False, result_scope: str = "ranked",
              completeness: bool = False, effort: str = "standard",
              understanding_ms: Optional[int] = 37) -> Callable[[Rig], AskRequest]:
    def build(rig: Rig) -> AskRequest:
        contract = QueryIntentContract(
            objective=question,
            resolved_question=question,
            intent_type="explain",
            result_scope=result_scope,
            completeness_required=completeness,
            entities=["版图"],
            mandatory_topics=[
                QueryIntentTopic(id=f"t{index}", title=f"方向{index}", question=topic)
                for index, topic in enumerate(topics)
            ],
            constraints=["先进工艺"],
            expected_output="要点清单",
            needs_clarification=clarified,
            confirmed=True,
        )
        answers = [QueryIntentAnswer(id="a1", answer="只看数字后端")] if clarified else []
        return AskRequest(
            question=question, mode="reasoning", asked_at=ASKED_AT,
            retrieval_effort=effort,
            intent=AskIntentConfirmation(
                contract=contract,
                resolved_question=(f"{question}(数字后端)" if clarified else question),
                answers=answers, understanding_ms=understanding_ms,
            ),
        )
    return build


# ------------------------------------------------------------- observation


@dataclasses.dataclass(frozen=True)
class Scenario:
    name: str
    world: Callable[[Rig], None]
    request: Callable[[Rig], AskRequest]
    expect: Callable[["Observation"], None]
    entry: str = "ask"                       # "ask" | "evidence" | "direct"
    stream: bool = False
    cancel_event: bool = True
    job: bool = False
    script: Script = Script()
    install: Optional[Callable[[Rig], None]] = None
    invoke: Optional[Callable[..., Any]] = None
    gap_plugin: bool = False
    env: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class Backend:
    """What differs between SQLite and PostgreSQL: everything else is shared."""

    name: str
    placeholder: str
    tables: Callable[[Any], list[str]]
    invalidate_caches: Callable[[], None]


@dataclasses.dataclass
class Observation:
    notebook_id: str
    user_id: str
    job_id: str
    outcome: dict
    timeline: list
    worker: list
    stream: list
    db_diff: dict
    logs: list
    response: Any = None
    error: Any = None
    #: The two request-scoped ContextVars once the Ask returned or raised.
    context_after: dict = dataclasses.field(default_factory=dict)

    def calls(self, key: str) -> list[dict]:
        return [e for e in self.timeline if e.get("call") == key]

    def call_names(self) -> list[str]:
        return [e["call"] for e in self.timeline if "call" in e]

    def models(self, workload: Optional[str] = None) -> list[dict]:
        return [e for e in self.timeline + self.worker if "model" in e
                and (workload is None or e["model"] == workload)]

    def stream_kinds(self) -> list[str]:
        return [step["step_type"] for step in self.stream]

    def trace_kinds(self) -> list[str]:
        return [step.step_type for step in (self.response.reasoning_trace or [])]

    def embedded(self) -> list[str]:
        """Every query text the run embedded, on any thread."""
        return [e["embed_query"] for e in self.timeline + self.worker if "embed_query" in e]


def _dump_db(repo: Any, backend: Backend) -> dict:
    out: dict[str, collections.Counter] = {}
    with repo._runtime.database.connect() as db:
        for table in backend.tables(repo):
            rows = db.execute(f"SELECT * FROM {table}").fetchall()
            out[table] = collections.Counter(_dumps(dict(row)) for row in rows)
    return out


def _db_diff(before: dict, after: dict) -> dict:
    """Per table, how many rows the run added and removed (an update is both)."""
    diff = {}
    for table in sorted(set(before) | set(after)):
        old = before.get(table, collections.Counter())
        new = after.get(table, collections.Counter())
        added, removed = sum((new - old).values()), sum((old - new).values())
        if added or removed:
            diff[table] = {"added": added, "removed": removed}
    return diff


class _LogCapture(logging.Handler):
    def __init__(self, recorder: Recorder) -> None:
        super().__init__(logging.DEBUG)
        self.recorder = recorder
        self.records: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        if not self.recorder.on_thread():
            return
        entry = {"logger": record.name, "level": record.levelname,
                 "message": self.recorder.canon(record.getMessage())}
        if record.exc_info and record.exc_info[0] is not None:
            entry["exc"] = record.exc_info[0].__name__
        self.records.append(entry)


#: Methods of the service the orchestrator (and its stages) call, recorded at
#: the boundary with normalised arguments and results.
_SELF_METHODS = (
    "_run_reasoning_stage", "_prepare_reasoning_ask", "_assert_reasoning_runtime",
    "_knowhow_completeness_in_scope", "_knowhow_catalog_in_ceiling", "_memory_hits",
    "_primary_llm_unconfigured", "_unconfigured_model_response",
    "_no_kg_scope_admits_run", "_build_reasoning_retriever",
    "_activate_selected_source_graph", "_spreadsheet_reasoning_results",
    "_response_draft_stage_for", "_draft_reasoning_response", "_draft_reasoning_evidence",
    "_attach_gap_consult_result", "_consult_gap_sources",
    "_draft_external_evidence_section", "_commit_reasoning_draft", "_save_answer",
    "_short_circuit_evidence",
)

#: Module functions the orchestrator imports at call time (or reads as globals).
_MODULE_FUNCTIONS = (
    ("app.application.ask_reasoning", ("execute_reasoning_retrieval_stage",
                                       "execute_response_draft_stage")),
    ("app.services.reasoning_retrieval", ("kg_in_scope_for",)),
    ("app.services.structured_retrieval", ("enumerate_knowhow",
                                           "is_knowhow_enumeration_query",
                                           "render_structured_answer",
                                           "supports_structured_knowhow_query")),
    ("app.services.ask_service", ("_egress_question",)),
)


def _install_recording(rig: Rig, monkeypatch: Any) -> None:
    import importlib

    recorder, hooks, service = rig.recorder, rig.hooks, rig.service
    for name in _SELF_METHODS:
        monkeypatch.setattr(service, name,
                            _recorded("self", name, getattr(service, name), recorder, hooks))
    for module_name, names in _MODULE_FUNCTIONS:
        module = importlib.import_module(module_name)
        for name in names:
            monkeypatch.setattr(module, name, _recorded(
                "fn", name, getattr(module, name), recorder, hooks))


#: The only process-level configuration a scenario runs with; everything else
#: is the shipped default plus the scenario's own ``env``.
_SCENARIO_ENV = {
    "SILICON_NOTEBOOK_ENV_FILE": "",
    "MODEL_SERVICES_CONFIG": "",
    "EXTENSIONS_CONFIG": "",
    "LLM_LOG_ENABLED": "false",
    "LLM_CACHE_ENABLED": "false",
    "EVENT_LOG_ENABLED": "false",
    "EMBED_DIM": str(EMBED_DIM),
}


def scrub_environment(monkeypatch: Any, tmp_path: Path, database_url: str) -> None:
    """No local ``.env`` and no deployment variable exported by the developer's
    shell may reach a scenario; every path points under this test's tmp_path."""
    import os

    from app.core.config import Settings

    names = set()
    for name, info in Settings.model_fields.items():
        names.add(name.lower())
        if isinstance(info.validation_alias, str):
            names.add(info.validation_alias.lower())
    for key in list(os.environ):
        if key.lower() in names:
            monkeypatch.delenv(key)
    for key, value in _SCENARIO_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("LLM_LOG_PATH", str(tmp_path / "logs" / "llm.jsonl"))


def _clear_process_caches(backend: Backend) -> None:
    from app.services import reasoning_retrieval

    with reasoning_retrieval._EXPERIENCE_CACHE_LOCK:
        reasoning_retrieval._EXPERIENCE_CACHE.clear()
    backend.invalidate_caches()


def make_gap_host():
    """A frozen ``ask.gap_consult`` host with one deterministic contribution."""
    from app.domain.gap_consult import GapSuggestion
    from app.extension_sdk import (
        ASK_GAP_CONSULT_POINT,
        EXTENSION_API_VERSION,
        ContributionDeclaration,
        ContributionKind,
        ContributorResult,
        ExtensionContribution,
        ExtensionManifest,
        ExtensionResultStatus,
    )
    from app.extension_sdk.gap_consult import SourceDescriptor
    from app.extensions.bootstrap import build_extension_runtime

    class _Plugin:
        def consult(self, context):
            return ContributorResult((GapSuggestion(
                "Physical Design Survey", "https://example.org/pd.pdf",
                "A survey of layout parasitics and corner signoff.", "arXiv"),),
                ExtensionResultStatus.AVAILABLE)

        def describe_sources(self):
            return (SourceDescriptor("corp_source", "测试站外来源"),)

    declaration = ContributionDeclaration(
        "corp.gap", ASK_GAP_CONSULT_POINT, ContributionKind.CONTRIBUTOR)

    @dataclasses.dataclass(frozen=True)
    class _Bundle:
        manifest: Any
        contribution: Any

        def register(self, registrar) -> None:
            registrar.add_contributor(self.contribution)

    bundle = _Bundle(
        ExtensionManifest(id="corp.gap", version="1.0.0", api_version=EXTENSION_API_VERSION,
                          display_name="corp.gap", trust="deployment",
                          contributions=(declaration,)),
        ExtensionContribution(declaration, _Plugin(), None),
    )
    return build_extension_runtime((bundle,)).gap_consult


def observe(repo: Any, backend: Backend, scenario: Scenario, tmp_path: Path,
            monkeypatch: Any) -> Observation:
    """Build the scenario's world, run one reasoning Ask with its collaborators
    recorded, and return what the behaviour assertions read."""
    canon = Canon()
    recorder = Recorder(canon)
    hooks = Hooks()
    models = ScriptedModels(recorder, scenario.script)
    rig = Rig(repo=repo, backend=backend, recorder=recorder, hooks=hooks, models=models,
              tmp_path=tmp_path)
    bind_all_embedding_clients(repo, RecordingEmbedder(recorder))
    for workload_id, workload in WORKLOADS.items():
        if workload.kind == "chat":
            bind_chat_client(repo, workload_id, ScriptedChat(workload_id, models))
    _clear_process_caches(backend)
    scenario.world(rig)
    _install_recording(rig, monkeypatch)
    if scenario.install is not None:
        scenario.install(rig)
    payload = scenario.request(rig)
    job_id = ""
    cancel = threading.Event() if scenario.cancel_event else None
    rig.cancel = cancel
    if scenario.job:
        job_id, _conversation = rig.service.ask_state.begin_durable_job(
            rig.notebook_id, payload, "reasoning", rig.user_id)
    _drain_background(repo)
    _clear_process_caches(backend)
    db_before = _dump_db(repo, backend)
    stream: list = []

    def on_trace(step) -> None:
        stream.append({"step_type": step.step_type, "summary": step.summary,
                       "detail": canon(step.detail or {})})
        hook = hooks.after.get("on_trace")
        if hook is not None:
            hook(step)

    loggers = [logging.getLogger("silicon_notebook"), logging.getLogger("app")]
    capture = _LogCapture(recorder)
    levels = [lg.level for lg in loggers]
    for lg in loggers:
        lg.addHandler(capture)
        lg.setLevel(logging.DEBUG)
    response = error = None
    try:
        recorder.active = True
        try:
            response = _invoke(rig, scenario, payload, job_id, cancel,
                               on_trace if scenario.stream else None)
            outcome = {"returned": type(response).__name__}
        except Exception as exc:  # noqa: BLE001 — the raise IS the observation
            error = exc
            outcome = {"raised": type(exc).__name__, "message": str(exc)}
        finally:
            recorder.active = False
    finally:
        for lg, level in zip(loggers, levels):
            lg.removeHandler(capture)
            lg.setLevel(level)
    from app.core.ask_context import _ASK_EMBED_CACHE, _ASK_MODEL_ERRORS

    context_after = {"model_errors": _ASK_MODEL_ERRORS.get(),
                     "embed_cache": _ASK_EMBED_CACHE.get()}
    _drain_background(repo)
    db_after = _dump_db(repo, backend)
    return Observation(
        notebook_id=rig.notebook_id, user_id=rig.user_id, job_id=job_id,
        outcome=outcome, timeline=recorder.timeline, worker=recorder.worker,
        stream=stream, db_diff=_db_diff(db_before, db_after), logs=capture.records,
        response=response, error=error, context_after=context_after,
    )


def _invoke(rig: Rig, scenario: Scenario, payload: AskRequest, job_id: str, cancel,
            on_trace):
    service = rig.service
    if scenario.entry == "evidence":
        return service.ask_evidence(rig.notebook_id, payload, user_id=rig.user_id,
                                    job_id=job_id, cancel_event=cancel, on_trace=on_trace)
    if scenario.invoke is not None:
        return scenario.invoke(rig, payload, job_id, cancel, on_trace)
    return service.ask(rig.notebook_id, payload, user_id=rig.user_id, job_id=job_id,
                       cancel_event=cancel, on_trace=on_trace)


def _drain_background(repo: Any) -> None:
    """Let pooled background work settle (before the run and before the row diff)."""
    del repo
    from tests.postgres.background_quiesce import quiesce_process_background_work

    quiesce_process_background_work()


# ------------------------------------------------------- the behaviour layer

NO_SOURCES = "当前笔记本没有可检索的来源"
UNCONFIGURED = "系统未配置当前问答所需的模型服务"
INCOMPLETE = "本次请求未完成，不能视为全部结果。"
UNVERIFIED = "本次回答未验证完整性，不能视为全部结果。"
NO_MEMORY = "未找到相关记忆"
METHODS = ("屏蔽走线", "插入缓冲", "降低负载")


def _common(obs: Observation) -> None:
    """Every exit -- answer, short circuit or raise -- resets both request-scoped
    ContextVars the orchestrator installs."""
    assert obs.context_after == {"model_errors": None, "embed_cache": None}, obs.context_after


def _added(obs: Observation, table: str) -> int:
    return obs.db_diff.get(table, {}).get("added", 0)


def _called(obs: Observation, key: str) -> bool:
    return bool(obs.calls(key))


def _answered(obs: Observation, *, llm_mode: str, kg_required: bool = False,
              job: bool = False) -> Any:
    """One reasoning answer, saved once under the prepared conversation (and job)."""
    _common(obs)
    assert obs.error is None, obs.outcome
    response = obs.response
    assert response.mode == "reasoning"
    assert response.llm_mode == llm_mode, response.llm_mode
    assert response.kg_required is kg_required
    assert response.answer_id and response.conversation_id
    assert _added(obs, "answers") == 1, obs.db_diff.keys()
    (save,) = obs.calls("self._save_answer")
    assert save["kwargs"]["job_id"] == obs.job_id
    assert bool(obs.job_id) is job
    assert response.asked_at == ASKED_AT
    return response


def _raised(obs: Observation, name: str, message: str = "") -> None:
    """The Ask raised: nothing answered, nothing saved."""
    _common(obs)
    assert obs.outcome.get("raised") == name, obs.outcome
    assert message in obs.outcome["message"], obs.outcome
    assert _added(obs, "answers") == 0
    assert not _called(obs, "self._save_answer")


def _short_circuit(obs: Observation) -> None:
    """A deterministic exit before retrieval: no retrieval, no draft, no model."""
    assert not _called(obs, "fn.execute_reasoning_retrieval_stage")
    assert not _called(obs, "fn.execute_response_draft_stage")
    assert obs.models() == []


def _ran_stages(obs: Observation) -> None:
    """Retrieval, then the draft stage, then the post-draft attach, once each."""
    names = obs.call_names()
    order = [names.index(key) for key in (
        "fn.execute_reasoning_retrieval_stage", "fn.execute_response_draft_stage",
        "self._attach_gap_consult_result", "self._commit_reasoning_draft")]
    assert order == sorted(order), names
    for key in ("fn.execute_reasoning_retrieval_stage", "fn.execute_response_draft_stage"):
        assert names.count(key) == 1, key


def _streamed_as_traced(obs: Observation) -> None:
    """Each step the response reports was streamed exactly once, in order."""
    assert obs.stream_kinds() == obs.trace_kinds(), (obs.stream_kinds(), obs.trace_kinds())


def _draft_input(obs: Observation) -> dict:
    (call,) = obs.calls("fn.execute_response_draft_stage")
    return call["args"][1]["dc:ResponseDraftInput"]


def _memory_step(response: Any) -> Any:
    first, second = response.reasoning_trace[:2]
    assert first.step_type == "intent"
    return second


def _between(obs: Observation, first: str, last: str) -> list[str]:
    names = obs.call_names()
    return names[names.index(first) + 1:names.index(last)]


#: How often a plain run embeds the research question in total. The scenarios
#: that pin it (``answer_sync``, ``direct_entry_without_run``) run in
#: ``library_world``, where the asker has no Memory: the Memory recall, which
#: runs before the run's embedding memo exists and embeds outside it, finds no
#: candidate with a vector and so does not embed at all -- only the retrieval
#: stage embeds the question, once, inside the memo. A world with a vector-
#: bearing Memory of the asker would add that recall's own embed (2).
QUESTION_EMBEDS_PER_RUN = 1


def _assert_embedded_once_in_memo(obs: Observation) -> None:
    """Every query text is embedded once inside the memo window; the research
    question's total is pinned apart (``QUESTION_EMBEDS_PER_RUN``)."""
    embedded = collections.Counter(obs.embedded())
    assert embedded[QUESTION] == QUESTION_EMBEDS_PER_RUN, embedded
    in_memo = embedded - collections.Counter({QUESTION: QUESTION_EMBEDS_PER_RUN - 1})
    assert set(in_memo.values()) <= {1}, embedded


def _prompt(obs: Observation, workload: str) -> str:
    return "\n".join(json.dumps(call["messages"], ensure_ascii=False)
                     for call in obs.models(workload))


def _expect_answer_sync(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    _ran_stages(obs)
    kinds = obs.trace_kinds()
    step = _memory_step(response)
    assert (step.step_type, step.summary) == ("skip", NO_MEMORY)
    assert step.duration_ms is not None
    assert "plan" in kinds and kinds[-1] == "synthesis", kinds
    assert obs.stream == []
    assert "[k1]" in response.answer and response.anchors
    assert response.gap_suggestions == [] and response.model_errors == []
    draft = _draft_input(obs)
    assert draft["kg_required"] is False and draft["memory_hits"] == []
    assert draft["structured_batch"] is None and draft["completeness_unavailable"] is False
    # The draft saw the whole trace the response reports, in the same order.
    assert [s["pd:TraceStep"]["step_type"] for s in draft["trace"]] == kinds[:-1]
    assert _called(obs, "fn.kg_in_scope_for")
    # The run's authority is re-verified right before the draft stage, not
    # only on entry and before persisting.
    points = [call["args"][1] for call in obs.calls("self._assert_reasoning_runtime")]
    assert points == ["prepare", "reasoning-stage-entry", "response-draft", "before-persist"]
    # The selected-source graph lane gets the configured passage cap.
    from app.core.config import Settings

    (activate,) = obs.calls("self._activate_selected_source_graph")
    assert activate["kwargs"]["max_results"] == Settings.model_fields["ppr_top_chunks"].default
    # Inside the run's embedding memo every query is embedded once.
    _assert_embedded_once_in_memo(obs)


def _expect_streamed_memory(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded", job=True)
    _ran_stages(obs)
    _streamed_as_traced(obs)
    step = _memory_step(response)
    assert (step.step_type, step.summary) == ("memory", "找到 1 条相关记忆")
    assert step.detail == {"count": 1}
    assert len(_draft_input(obs)["memory_hits"]) == 1
    # A memory hit is usable evidence: the graph probe is skipped.
    assert "fn.kg_in_scope_for" not in _between(
        obs, "self._primary_llm_unconfigured", "self._build_reasoning_retriever")
    assert obs.trace_kinds().count("retrieve") == 2  # the scripted add_subquery ran
    assert response.reasoning_trace[0].duration_ms == 37  # the client's understanding_ms


def _expect_clarified(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    _ran_stages(obs)
    _streamed_as_traced(obs)
    assert response.reasoning_trace[2].summary == "采用已确认意图的 2 个检索方向"
    (egress,) = obs.calls("fn._egress_question")
    assert egress["ret"] == QUESTION
    (build,) = obs.calls("self._build_reasoning_retriever")
    assert build["kwargs"]["egress_question"] == QUESTION
    assert response.intent.confirmed is True


def _expect_evidence(obs: Observation) -> None:
    _common(obs)
    assert obs.error is None, obs.outcome
    evidence = obs.response
    assert type(evidence).__name__ == "AskEvidence"
    assert evidence.mode == "reasoning" and evidence.evidence_kind == "retrieval"
    assert evidence.items and evidence.counts.delivered > 0
    assert obs.db_diff == {}
    assert obs.models("ask_answer") == []
    assert _called(obs, "self._draft_reasoning_evidence")
    assert not _called(obs, "self._draft_reasoning_response")
    assert _called(obs, "self._attach_gap_consult_result")
    assert not _called(obs, "self._consult_gap_sources")


def _expect_evidence_stream(obs: Observation) -> None:
    _expect_evidence(obs)
    assert obs.stream_kinds()[:3] == ["intent", "skip", "plan"]


def _expect_evidence_gap(obs: Observation) -> None:
    _expect_evidence(obs)
    assert obs.models("gap_consult_query") == []
    assert obs.stream == []


def _expect_evidence_empty(obs: Observation) -> None:
    _common(obs)
    evidence = obs.response
    assert evidence.evidence_kind == "none" and evidence.notice.startswith(NO_SOURCES)
    assert obs.db_diff == {}
    _short_circuit(obs)


def _expect_no_graph(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded", kg_required=True)
    _ran_stages(obs)
    _streamed_as_traced(obs)
    assert _draft_input(obs)["kg_required"] is True
    (admits,) = obs.calls("self._no_kg_scope_admits_run")
    assert admits["ret"] is True
    assert "[k1]" in response.answer


def _expect_empty_streamed(obs: Observation) -> None:
    response = _answered(obs, llm_mode="deterministic", kg_required=True, job=True)
    _short_circuit(obs)
    assert response.conclusion.startswith(NO_SOURCES) and response.completeness_notice == ""
    assert obs.trace_kinds() == ["intent"] and obs.stream_kinds() == ["intent"]
    assert response.grounded is False and response.evidence_level == "inferred"


def _expect_empty_completeness(obs: Observation) -> None:
    response = _answered(obs, llm_mode="deterministic", kg_required=True)
    _short_circuit(obs)
    assert response.completeness_notice == INCOMPLETE
    assert response.conclusion.startswith(NO_SOURCES)
    assert response.conclusion.endswith(INCOMPLETE)
    assert response.reasoning_trace is None and obs.stream == []


def _expect_unconfigured(stream: bool, job: bool = False) -> Callable[[Observation], None]:
    def expect(obs: Observation) -> None:
        response = _answered(obs, llm_mode="deterministic", job=job)
        _short_circuit(obs)
        assert response.conclusion.startswith(UNCONFIGURED)
        assert [(e.stage, e.message) for e in response.model_errors] == [
            ("answer", "missing_config")]
        # Memory was read, but an Ask that answers nothing records no memory step.
        assert len(obs.calls("self._memory_hits")) == 1
        if stream:
            assert obs.trace_kinds() == ["intent"] and obs.stream_kinds() == ["intent"]
        else:
            assert response.reasoning_trace is None and obs.stream == []
    return expect


def _expect_structured(*, aggregate: bool, stream: bool, job: bool = False,
                       legacy: bool = False) -> Callable[[Observation], None]:
    def expect(obs: Observation) -> None:
        response = _answered(obs, llm_mode="structured", job=job)
        _short_circuit(obs)
        assert response.conclusion == (
            "已完整统计 3 行 Knowhow 记录。" if aggregate else "已完整读取 3/3 行 Knowhow 记录。")
        assert all(method in response.answer for method in METHODS)
        assert response.grounded is True and response.evidence_level == "grounded"
        assert response.result_coverage.complete is True
        assert response.result_coverage.returned_rows == 3 and response.result_sets
        assert obs.trace_kinds() == ["intent", "enumerate", "answer"]
        assert response.reasoning_trace[2].summary == "完整枚举 3 行"
        assert response.reasoning_trace[2].detail == {
            "complete": True, "scanned_rows": 3, "returned_rows": 3,
            "known_total_rows": 3, "truncated_reason": ""}
        assert obs.stream_kinds() == (obs.trace_kinds() if stream else [])
        # The structured answer returns before memory is ever read.
        assert not _called(obs, "self._memory_hits")
        (enumerate_call,) = obs.calls("fn.enumerate_knowhow")
        snapshot = enumerate_call["kwargs"]["catalog_snapshot"]
        assert snapshot["known_total_rows"] == 3
        assert (snapshot["fingerprint"] == []) is legacy
    return expect


def _expect_hybrid_answer(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    _ran_stages(obs)
    _streamed_as_traced(obs)
    assert obs.trace_kinds()[:3] == ["intent", "enumerate", "skip"]
    draft = _draft_input(obs)
    assert draft["structured_batch"] is not None
    assert draft["completeness_unavailable"] is False
    assert "Knowhow 枚举：3/3 行" in response.answer
    assert response.completeness_notice == ""


def _expect_hybrid_unconfigured(obs: Observation) -> None:
    response = _answered(obs, llm_mode="structured")
    _short_circuit(obs)
    assert response.conclusion.startswith("已完整读取 3/3 行 Knowhow 记录。")
    assert "分析阶段未运行" in response.conclusion
    assert all(method in response.answer for method in METHODS)
    assert [(e.stage, e.message) for e in response.model_errors] == [
        ("answer", "missing_config")]
    assert obs.trace_kinds() == ["intent", "enumerate"] == obs.stream_kinds()
    assert response.grounded is True and response.result_sets


def _expect_hybrid_no_sources(obs: Observation) -> None:
    response = _answered(obs, llm_mode="deterministic", kg_required=True)
    _short_circuit(obs)
    assert response.conclusion.startswith("已完整读取 3/3 行 Knowhow 记录。\n\n" + NO_SOURCES)
    assert all(method in response.answer for method in METHODS)
    assert response.grounded is True and response.evidence_level == "grounded"
    assert response.completeness_notice == "" and response.result_sets
    assert obs.trace_kinds() == ["intent", "enumerate"] == obs.stream_kinds()


def _expect_unverified(*, probed: bool, stream: bool) -> Callable[[Observation], None]:
    def expect(obs: Observation) -> None:
        response = _answered(obs, llm_mode="grounded")
        _ran_stages(obs)
        assert response.completeness_notice == UNVERIFIED
        draft = _draft_input(obs)
        assert draft["completeness_unavailable"] is True and draft["structured_batch"] is None
        assert not _called(obs, "fn.enumerate_knowhow")
        assert _called(obs, "fn.is_knowhow_enumeration_query") is probed
        assert "enumerate" not in obs.trace_kinds()
        if stream:
            _streamed_as_traced(obs)
    return expect


def _expect_fail_soft(obs: Observation) -> None:
    response = _answered(obs, llm_mode="deterministic")
    _streamed_as_traced(obs)
    # The failed retrieval contributes nothing: only the pre-retrieval steps,
    # and the deterministic no-evidence answer.
    assert obs.trace_kinds() == ["intent", "skip"]
    assert response.answer == "" and response.grounded is False
    draft = _draft_input(obs)
    for key in ("top_hits", "elements", "chunks", "chains", "enumerations",
                "document_reads", "outline", "outline_evidence", "historical_chunks"):
        assert draft[key] == [], key
    assert draft["collection_map_text"] == "" and draft["candidate_manifest"] is None
    assert not _called(obs, "self._activate_selected_source_graph")
    assert _called(obs, "self._spreadsheet_reasoning_results")
    assert obs.models("reasoning_agent") == []


def _expect_reraised(name: str, message: str) -> Callable[[Observation], None]:
    def expect(obs: Observation) -> None:
        _raised(obs, name, message)
        assert not _called(obs, "self._spreadsheet_reasoning_results")
        assert not _called(obs, "fn.execute_response_draft_stage")
    return expect


def _expect_cancel_mid_retrieval(obs: Observation) -> None:
    _raised(obs, "AskCancelled")
    # Cancelled when the reflect step streams (the first round's pool is done
    # by then): the next step the loop hands over raises.
    assert obs.stream_kinds()[-1] == "reflect" and "answer" not in obs.stream_kinds()
    assert obs.models("ask_answer") == []
    assert not _called(obs, "self._spreadsheet_reasoning_results")


def _expect_cancel_before_draft(obs: Observation) -> None:
    _raised(obs, "AskCancelled")
    assert _called(obs, "self._spreadsheet_reasoning_results")
    assert not _called(obs, "self._response_draft_stage_for")
    points = [call["args"][1] for call in obs.calls("self._assert_reasoning_runtime")]
    assert "response-draft" not in points, points


def _expect_cancel_at_intent(obs: Observation) -> None:
    _raised(obs, "AskCancelled")
    assert obs.stream_kinds() == ["intent"]
    assert not _called(obs, "self._memory_hits")


def _expect_answer_model_failed(stream: bool) -> Callable[[Observation], None]:
    def expect(obs: Observation) -> None:
        response = _answered(obs, llm_mode="synthesis_failed")
        _ran_stages(obs)
        assert response.answer == ""
        assert response.model_errors and all(
            (e.stage, e.workload_id) == ("answer", "ask_answer") for e in response.model_errors)
        if stream:
            _streamed_as_traced(obs)
        else:
            assert obs.stream == []
    return expect


def _expect_planner_failed(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    _ran_stages(obs)
    assert obs.models("reasoning_agent") and all(
        call.get("raised") == "RuntimeError" for call in obs.models("reasoning_agent"))
    assert "plan" in obs.trace_kinds() and "[k1]" in response.answer


def _expect_gap(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    _ran_stages(obs)
    _streamed_as_traced(obs)
    assert [s.title for s in response.gap_suggestions] == ["Physical Design Survey"]
    assert response.gap_egress["query"] == QUESTION
    assert "[X1]" in response.external_evidence.text
    assert len(response.external_evidence.conflicts) == 1
    assert obs.trace_kinds()[-3:] == ["synthesis", "gap_consult", "external_evidence"]
    # The draft cannot have seen the outside material.
    assert "Physical Design Survey" not in _prompt(obs, "ask_answer")
    # Only what was noted before the draft returned is the answer's model
    # errors; an alarm raised while the outside sources are consulted is not.
    assert response.model_errors == []


def _expect_cancel_at_memory(obs: Observation) -> None:
    _raised(obs, "AskCancelled")
    # The memory step is checked before it is recorded or streamed, so a Stop
    # during the recall ends the run before retrieval.
    assert obs.stream_kinds() == ["intent"]
    assert not _called(obs, "fn.execute_reasoning_retrieval_stage")


def _expect_sink_checks_cancel(obs: Observation) -> None:
    _raised(obs, "AskCancelled")
    # The trace sink handed to retrieval checks the Stop before it streams.
    assert PROBE_STEP not in [step["summary"] for step in obs.stream]
    assert obs.models("reasoning_agent") == []


def _expect_injected(obs: Observation) -> None:
    response = _answered(obs, llm_mode="llm")
    _ran_stages(obs)
    _streamed_as_traced(obs)
    assert response.answer.startswith("注入阶段收到")
    assert not _called(obs, "self._draft_reasoning_response")
    # The core copies the request's model-error sink onto the stage's answer.
    assert [(e.stage, e.workload_id) for e in response.model_errors] == [
        ("model_call", "reasoning_agent")]


def _expect_injected_foreign(obs: Observation) -> None:
    _raised(obs, "StageBoundaryError", "invalid reasoning response graph")
    assert _called(obs, "fn.execute_response_draft_stage")
    assert not _called(obs, "self._attach_gap_consult_result")


def _expect_workbook(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    _ran_stages(obs)
    _streamed_as_traced(obs)
    kinds = obs.trace_kinds()
    assert kinds[-2:] == ["spreadsheet", "synthesis"], kinds
    assert _draft_input(obs)["spreadsheet_results"]
    assert response.answer


def _expect_workbook_failed(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    _ran_stages(obs)
    assert "spreadsheet" not in obs.trace_kinds()
    assert _draft_input(obs)["spreadsheet_results"] == []
    assert {"logger": "silicon_notebook.events", "level": "WARNING",
            "message": "spreadsheet reasoning lane failed (ValueError)"} in obs.logs
    assert response.answer


def _expect_invalid_inputs(obs: Observation) -> None:
    _common(obs)
    assert obs.response == {
        "prepared": "StageBoundaryError: invalid prepared Ask reasoning input",
        "runtime": "StageBoundaryError: invalid Ask reasoning runtime",
    }
    assert not _called(obs, "self._memory_hits")
    assert not _called(obs, "fn.execute_reasoning_retrieval_stage")


# ------------------------------------------------------------- installers


def _unconfigured(rig: Rig) -> None:
    rig.hooks.replace["self._primary_llm_unconfigured"] = lambda real: True


def _knowhow_out_of_scope(rig: Rig) -> None:
    rig.hooks.replace["self._knowhow_completeness_in_scope"] = lambda real, nb: False


class _LegacyKnowhowStore:
    """A narrow double without ``knowhow_enumeration_catalog`` (the legacy branch)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        if name == "knowhow_enumeration_catalog":
            raise AttributeError(name)
        return getattr(self._inner, name)


def _legacy_knowhow(rig: Rig) -> None:
    service = rig.service
    service.knowhow_store = _LegacyKnowhowStore(service.knowhow_store)


def _retrieval_raises(exc_factory: Callable[[], BaseException]) -> Callable[[Rig], None]:
    def install(rig: Rig) -> None:
        def boom(real, *args, **kwargs):
            raise exc_factory()
        rig.hooks.replace["fn.execute_reasoning_retrieval_stage"] = boom
    return install


def _runtime_error() -> BaseException:
    return RuntimeError("retrieval blew up")


def _control_error() -> BaseException:
    from app.domain.retrieval_control import RetrievalControlError

    return RetrievalControlError("participant override refused")


def _boundary_error() -> BaseException:
    from app.application.ask_reasoning import StageBoundaryError

    return StageBoundaryError("retrieval stage boundary")


def _cancel_on_stream(step_type: str) -> Callable[[Rig], None]:
    def install(rig: Rig) -> None:
        def on_step(step) -> None:
            if step.step_type == step_type:
                rig.cancel.set()
        rig.hooks.after["on_trace"] = on_step
    return install


def _cancel_after(key: str) -> Callable[[Rig], None]:
    def install(rig: Rig) -> None:
        rig.hooks.after[key] = lambda result, *a, **k: rig.cancel.set()
    return install


def _injected_stage(foreign: bool) -> Callable[[Rig], None]:
    def install(rig: Rig) -> None:
        from app.application.ask_reasoning import ReasoningResponseDraft
        from app.models.ask import AskResponse

        class _Stage:
            def draft_response(self, stage, runtime):
                prepared = stage.prepared
                response = None if foreign else AskResponse(
                    answer_id="", conclusion="注入阶段的答案",
                    answer=f"注入阶段收到 {len(stage.trace)} 步轨迹、"
                           f"{len(stage.chunks)} 段原文。",
                    grounded=False, evidence_level="inferred", llm_mode="llm",
                    conversation_id=prepared.conversation_id,
                    retrieval_query=prepared.research_question,
                    intent=stage.intent_contract,
                    retrieval_effort=prepared.retrieval_effort,
                    reasoning_trace=list(stage.trace),
                )
                if response is not None:
                    response.mode = "reasoning"
                return ReasoningResponseDraft(
                    notebook_id=prepared.notebook_id, question=prepared.question,
                    response=response, conversation_id=prepared.conversation_id,
                    user_id=prepared.user_id, job_id=prepared.job_id,
                    asked_at=prepared.asked_at,
                )

        rig.service.response_draft_stage = _Stage()
        # A retrieval-side alarm lands in the request's model-error sink while
        # it is installed; the core (not the stage) copies it onto the answer.
        rig.hooks.before["self._spreadsheet_reasoning_results"] = (
            lambda *a, **k: rig.repo._runtime.models.note_model_error(
                "reflect", RuntimeError("reflect down"), workload_id="reasoning_agent"))
    return install


def _workbook_lane_fails(rig: Rig) -> None:
    def boom(real, **kwargs):
        raise ValueError("workbook lane broke")
    analysis = rig.service.spreadsheet_analysis
    original = analysis.analyze
    rig.hooks.replace["sheet.analyze"] = boom
    rig.service.spreadsheet_analysis = _AnalysisSpy(analysis, original, rig)


class _AnalysisSpy:
    def __init__(self, inner: Any, analyze: Callable, rig: Rig) -> None:
        self._inner = inner
        self.analyze = _recorded("sheet", "analyze", analyze, rig.recorder, rig.hooks)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _invalid_inputs(rig: Rig, payload, job_id, cancel, on_trace):
    """The orchestrator's own type gate, reached directly (no entry builds these)."""
    from app.application.ask_reasoning import ReasoningRetrievalRuntime

    service = rig.service
    runtime = ReasoningRetrievalRuntime(
        scope=None, retrieval_run=None, cancellation=cancel, trace_sink=on_trace,
        connection_probe=service.retrieval_connection_probe,
    )
    outcomes = {}
    try:
        service._run_reasoning_stage("not prepared", runtime)
    except Exception as exc:  # noqa: BLE001 — the raise IS the observation
        outcomes["prepared"] = f"{type(exc).__name__}: {exc}"
    prepared = service._prepare_reasoning_ask(
        rig.notebook_id, payload, user_id=rig.user_id, job_id=job_id, runtime=runtime)
    try:
        service._run_reasoning_stage(prepared, "not a runtime")
    except Exception as exc:  # noqa: BLE001 — the raise IS the observation
        outcomes["runtime"] = f"{type(exc).__name__}: {exc}"
    return outcomes


def _without_run(rig: Rig, payload, job_id, cancel, on_trace):
    """``ask_reasoning`` called directly, outside ``_engine_scope``: no retrieval
    run (and so no run-level embedding memo), the orchestrator's own only."""
    return rig.service.ask_reasoning(rig.notebook_id, payload, user_id=rig.user_id,
                                     job_id=job_id, on_trace=on_trace, cancel_event=cancel)


def _expect_without_run(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    _ran_stages(obs)
    # The orchestrator's embedding memo is the only one here: inside it each
    # query is embedded once.
    _assert_embedded_once_in_memo(obs)
    assert "[k1]" in response.answer


def _narrowed(rig: Rig) -> AskRequest:
    """The library Ask with the sign-off document unticked: a narrowed source
    scope, the only kind of run that captures a retrieval baseline manifest."""
    from app.models.source_scope import SourceScope

    return AskRequest(question=QUESTION, mode="reasoning", asked_at=ASKED_AT,
                      source_scope=SourceScope(mode="exclude",
                                               source_ids=[rig.sources["signoff.md"]]))


#: What a plugin reflect action brought back (admitted and keyed by the core).
EXTERNAL_ITEM = ("ext:acme.search:1", "acme.search", "search_papers", "外部论文库",
                 "版图寄生综述", "外部摘录:先进工艺版图寄生提取的常见做法。",
                 "https://example.org/p1", "§1")


def _snapshot_extras(rig: Rig) -> None:
    """The retriever's snapshot carries a candidate ledger with capture errors
    and one item of external evidence; the selected-source graph lane hands
    back the passages in a different order than retrieval found them."""
    from app.domain.reflect_action import ExternalEvidence

    def snapshot(real, *args, **kwargs):
        result = real(*args, **kwargs)
        manifest = result.baseline_manifest
        assert dataclasses.is_dataclass(manifest), type(manifest)
        return dataclasses.replace(
            result,
            baseline_manifest=dataclasses.replace(manifest, capture_error_count=2),
            external_evidence=(ExternalEvidence(*EXTERNAL_ITEM),),
        )

    def activate(real, notebook_id, chunks, **kwargs):
        selected, status = real(notebook_id, chunks, **kwargs)
        return list(reversed(selected)), status

    rig.hooks.replace["fn.execute_reasoning_retrieval_stage"] = snapshot
    rig.hooks.replace["self._activate_selected_source_graph"] = activate


def _expect_snapshot_extras(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    _ran_stages(obs)
    # The orchestrator's own result: the retriever's ledger is the draft's prior
    # manifest, so its capture errors carry into the final one the commit returns.
    (stage,) = obs.calls("self._run_reasoning_stage")
    committed = stage["ret"]["dc:CommittedReasoningAnswer"]
    manifest = committed["baseline_manifest"]["dc:RetrievalBaselineManifest"]
    assert manifest["capture_error_count"] >= 2, manifest["capture_error_count"]
    draft = _draft_input(obs)
    assert draft["candidate_manifest"] is not None
    assert [item["dc:ExternalEvidence"]["key"] for item in draft["external_evidence"]] == [
        EXTERNAL_ITEM[0]]
    assert "版图寄生综述" in _prompt(obs, "ask_answer")
    # Historical chunks are the retrieval's order; chunks are the lane's.
    historical = [c["dc:RetrievedChunk"]["chunk_id"] for c in draft["historical_chunks"]]
    activated = [c["dc:RetrievedChunk"]["chunk_id"] for c in draft["chunks"]]
    assert len(historical) > 1 and activated == list(reversed(historical))
    assert response.answer


def _activation_fails(rig: Rig) -> None:
    """Retrieval returns, then the selected-source graph lane blows up."""
    _snapshot_extras(rig)

    def boom(real, *args, **kwargs):
        raise RuntimeError("selected-source graph lane broke")

    rig.hooks.replace["self._activate_selected_source_graph"] = boom


def _expect_activation_fails(obs: Observation) -> None:
    # Only the outside material is left to answer from: an ungrounded answer.
    response = _answered(obs, llm_mode="ungrounded")
    _ran_stages(obs)
    # Fail-soft after the retriever returned: the evidence lists are emptied
    # and the trace is back to the pre-retrieval steps, but the retriever's
    # own result (ledger, external evidence) and the passages it found before
    # the lane ran still reach the draft.
    draft = _draft_input(obs)
    assert [s["pd:TraceStep"]["step_type"] for s in draft["trace"]] == ["intent", "skip"]
    assert "plan" not in obs.trace_kinds()
    for key in ("top_hits", "elements", "chunks", "chains", "enumerations",
                "document_reads", "outline", "outline_evidence"):
        assert draft[key] == [], key
    assert draft["candidate_manifest"] is not None
    assert [item["dc:ExternalEvidence"]["key"] for item in draft["external_evidence"]] == [
        EXTERNAL_ITEM[0]]
    assert len(draft["historical_chunks"]) > 1
    assert response.grounded is False


def _expect_unconfigured_completeness(obs: Observation) -> None:
    _expect_unconfigured(stream=False)(obs)
    response = obs.response
    assert response.completeness_notice == INCOMPLETE, response.completeness_notice


PROBE_STEP = "探针步"


def _sink_probe(rig: Rig) -> None:
    """Stop, then hand a step to the retrieval runtime's trace sink."""
    from app.models.ask import TraceStep

    def probe(real, retriever, stage, runtime):
        rig.cancel.set()
        runtime.trace_sink(TraceStep(step_type="plan", summary=PROBE_STEP))
        return real(retriever, stage, runtime)

    rig.hooks.replace["fn.execute_reasoning_retrieval_stage"] = probe


def _gap_alarm(rig: Rig) -> None:
    """A model alarm noted while the outside sources are being consulted."""
    rig.hooks.before["self._consult_gap_sources"] = (
        lambda *a, **k: rig.repo._runtime.models.note_model_error(
            "gap_consult", RuntimeError("gap down"), workload_id="gap_consult_query"))


def _model_timeout() -> BaseException:
    from app.services.model_work import ModelQueueTimeout

    return ModelQueueTimeout("answer queue timed out")


LIST_ALL = "列出所有方法"
SHEET_QUESTION = "统计表格中各路径 Slack 的平均值"
COUNT_ALL = "方法一共有多少个"
SUB_QUERY = {"next_action": "add_subquery", "reason": "补查寄生提取",
             "new_sub_query": {"query": "寄生提取与屏蔽", "types": [], "prefer": "balanced",
                               "reason": "补查"}}

SCENARIOS = [
    Scenario("answer_sync", library_world, plain(), _expect_answer_sync),
    Scenario("answer_streamed_job_with_memory", memory_world,
             confirmed(topics=("寄生电容如何控制",)), _expect_streamed_memory,
             stream=True, job=True, script=Script(reflects=(SUB_QUERY,))),
    Scenario("clarified_intent_streamed", library_world, confirmed(clarified=True),
             _expect_clarified, stream=True),
    Scenario("evidence_output", library_world, plain(), _expect_evidence_stream,
             entry="evidence", stream=True),
    Scenario("evidence_short_circuit_empty", empty_world, plain(), _expect_evidence_empty,
             entry="evidence"),
    Scenario("no_graph_documents", documents_world, plain(), _expect_no_graph, stream=True),
    Scenario("empty_notebook_streamed", empty_world, plain(), _expect_empty_streamed,
             stream=True, job=True),
    Scenario("empty_notebook_completeness_sync", empty_world,
             confirmed(LIST_ALL, topics=(), result_scope="complete", completeness=True),
             _expect_empty_completeness),
    Scenario("unconfigured_streamed", library_world, plain(), _expect_unconfigured(stream=True),
             stream=True, install=_unconfigured),
    Scenario("unconfigured_sync_job", library_world, confirmed(), _expect_unconfigured(stream=False, job=True),
             job=True, install=_unconfigured),
    Scenario("knowhow_complete_streamed_job", knowhow_world,
             confirmed(LIST_ALL, topics=(), result_scope="complete", completeness=True),
             _expect_structured(aggregate=False, stream=True, job=True), stream=True, job=True),
    Scenario("knowhow_aggregate_sync", knowhow_world,
             confirmed(COUNT_ALL, topics=(), result_scope="aggregate", completeness=True),
             _expect_structured(aggregate=True, stream=False)),
    Scenario("knowhow_hybrid_answer", knowhow_world,
             confirmed(LIST_ALL, topics=(), result_scope="hybrid", completeness=True),
             _expect_hybrid_answer, stream=True),
    Scenario("knowhow_hybrid_unconfigured", knowhow_world,
             confirmed(LIST_ALL, topics=(), result_scope="hybrid", completeness=True),
             _expect_hybrid_unconfigured, stream=True, install=_unconfigured),
    Scenario("knowhow_hybrid_no_sources", knowhow_only_world,
             confirmed(LIST_ALL, topics=(), result_scope="hybrid", completeness=True),
             _expect_hybrid_no_sources, stream=True),
    Scenario("knowhow_not_enumerable", knowhow_world,
             confirmed("对比两种方法的优劣", topics=(), result_scope="complete",
                       completeness=True),
             _expect_unverified(probed=True, stream=True), stream=True),
    Scenario("knowhow_legacy_catalog", knowhow_world,
             confirmed(LIST_ALL, topics=(), result_scope="complete", completeness=True),
             _expect_structured(aggregate=False, stream=False, legacy=True), install=_legacy_knowhow),
    Scenario("knowhow_out_of_scope", knowhow_world,
             confirmed(LIST_ALL, topics=(), result_scope="complete", completeness=True),
             _expect_unverified(probed=False, stream=False), install=_knowhow_out_of_scope),
    Scenario("retrieval_fails_soft", library_world, plain(), _expect_fail_soft,
             stream=True, install=_retrieval_raises(_runtime_error)),
    Scenario("retrieval_control_error", library_world, plain(), _expect_reraised("RetrievalControlError", "participant override refused"),
             install=_retrieval_raises(_control_error)),
    Scenario("retrieval_boundary_error", library_world, plain(), _expect_reraised("StageBoundaryError", "retrieval stage boundary"),
             install=_retrieval_raises(_boundary_error)),
    Scenario("cancelled_mid_retrieval", library_world, plain(), _expect_cancel_mid_retrieval,
             stream=True, install=_cancel_on_stream("reflect")),
    Scenario("cancelled_before_draft", library_world, plain(), _expect_cancel_before_draft,
             install=_cancel_after("self._spreadsheet_reasoning_results")),
    Scenario("cancelled_at_intent_stream", knowhow_world,
             confirmed(LIST_ALL, topics=(), result_scope="complete", completeness=True),
             _expect_cancel_at_intent, stream=True, install=_cancel_on_stream("intent")),
    Scenario("cancelled_at_memory_step", library_world, plain(), _expect_cancel_at_memory,
             stream=True, install=_cancel_after("self._memory_hits")),
    Scenario("retrieval_sink_checks_cancel", library_world, plain(),
             _expect_sink_checks_cancel, stream=True, install=_sink_probe),
    Scenario("answer_model_fails", library_world, plain(), _expect_answer_model_failed(stream=True), stream=True,
             script=Script(failures={"ask_answer": RuntimeError("answer model down")})),
    Scenario("answer_model_timeout", library_world, plain(), _expect_answer_model_failed(stream=False),
             script=Script(failures={"ask_answer": _model_timeout()})),
    Scenario("planner_fails", library_world, plain(), _expect_planner_failed,
             script=Script(failures={"reasoning_agent": RuntimeError("planner down")})),
    Scenario("gap_consult_attached", library_world,
             confirmed(topics=("寄生电容如何控制", "工艺角怎么选", "布线层怎么分配",
                               "签核顺序是什么"), effort="overview"),
             _expect_gap, stream=True, gap_plugin=True, install=_gap_alarm),
    Scenario("gap_consult_evidence_output", library_world, plain(), _expect_evidence_gap,
             entry="evidence", gap_plugin=True),
    Scenario("injected_stage_response", library_world, plain(), _expect_injected,
             stream=True, install=_injected_stage(foreign=False)),
    Scenario("injected_stage_foreign_response", library_world, plain(), _expect_injected_foreign,
             install=_injected_stage(foreign=True)),
    Scenario("workbook_lane", workbook_world, plain(SHEET_QUESTION),
             _expect_workbook, stream=True),
    Scenario("workbook_lane_fails", workbook_world, plain(SHEET_QUESTION),
             _expect_workbook_failed, install=_workbook_lane_fails),
    Scenario("retrieval_snapshot_extras", library_world, _narrowed, _expect_snapshot_extras,
             install=_snapshot_extras),
    Scenario("direct_entry_without_run", library_world, plain(), _expect_without_run,
             invoke=_without_run),
    Scenario("activation_fails_soft", library_world, _narrowed, _expect_activation_fails,
             install=_activation_fails),
    Scenario("unconfigured_completeness", library_world,
             confirmed(LIST_ALL, topics=(), result_scope="complete", completeness=True),
             _expect_unconfigured_completeness, install=_unconfigured),
    Scenario("invalid_stage_inputs", library_world, plain(), _expect_invalid_inputs,
             invoke=_invalid_inputs),
]
