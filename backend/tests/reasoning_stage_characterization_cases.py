"""Behaviour rig for the reasoning Ask: the orchestrator ``AskService._run_reasoning_stage``
and the shipped response draft stage ``AskService._draft_reasoning_response``.

**两端吃同一份世界、同一套观测、同一组场景**:``tests/test_reasoning_stage_characterization.py``
(SQLite)与 ``tests/postgres/test_reasoning_stage_characterization_pg.py``(PostgreSQL)各自只写
一个 ``Backend`` 适配器(有哪些表、缓存怎么清)。不是测试模块(没有 ``test_`` 前缀),pytest 不会
收集它。

每个场景经生产入口(``AskService.ask`` / ``AskService.ask_evidence``,即 ``_engine_scope`` 装好
检索天花板与 retrieval run 之后)跑一次真实的逐步推理 Ask:真实仓库、真实检索与合成,只有模型是
脚本化的确定性替身。``observe`` 录下断言要读的东西:对服务自身协作方法与 ``execute_*_stage`` /
``kg_in_scope_for`` / 结构化枚举 / ``_egress_question`` / 大纲分节规划的调用,以及草稿阶段经
``evidence_context`` / ``schemas`` / ``model_clients`` 端口的调用(规范化后的参数与返回值)、模型收到
的提示词、查询 embedding、流式推送的每一步、本线程日志、表的增删行数、两个请求级 ContextVar 在
调用之后的值,以及返回值或异常。``Scenario.expect`` 断言用户与下游可见的事实——走了哪条短路、
``llm_mode`` / ``kg_required`` / 完整性披露、轨迹的步骤序列与流式推送、记忆那一步、模型错误、站外
补充、答案与会话是否落库、取消与异常的类型。

这套世界与场景两次充当零行为变化拆分的表征:先是 ``_run_reasoning_stage``,再是草稿阶段
``_draft_reasoning_response``(为它补了 ``DRAFT_SCENARIOS`` 那一组分支)。两次都是拆分前在两端采集
的逐字节快照(另含每次取消检查、ContextVar 的 set/reset、事件日志、本线程的每条 SQL 与整库逐行差分)
拆分后不变,随后按 AGENTS.md「不做只为重构服务的金标快照」退役,只留本行为层。
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
_EVIDENCE_KEY = re.compile(r"(?m)^(k\d+): ")


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
        if workload == "ask_answer" and any(
                marker in prompt for marker in self.script.answer_fails_on):
            failure = RuntimeError("section answer model down")
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
            if callable(item):  # a reflect that reads the candidates it is shown
                item = item(prompt)
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
            if script.cite is None:
                keys = list(dict.fromkeys(_KEY.findall(prompt)))[:3]
            else:
                tail = prompt.split(script.cite_after, 1)[-1] if script.cite_after else prompt
                keys = [key for key in dict.fromkeys(_EVIDENCE_KEY.findall(tail))
                        if re.fullmatch(script.cite, key)][:3]
            if any(marker in prompt for marker in script.uncited_on):
                keys = []
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
    #: None: the answer model cites the first three ``[k]`` markers its prompt
    #: mentions; a pattern: the first three evidence keys (``k12: ...`` lines)
    #: that match it.
    cite: Optional[str] = None
    #: An answer prompt holding one of these raises (that section's model is down).
    answer_fails_on: tuple = ()
    #: An answer prompt holding one of these is answered without any citation.
    uncited_on: tuple = ()
    #: With ``cite``: only the evidence keys after this marker are citable.
    cite_after: str = ""


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

    @property
    def json_placeholder(self) -> str:
        return "%s::jsonb" if self.backend.placeholder == "%s" else "?"

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


def _layout_citer(rig: Rig) -> Callable[[str], list[dict]]:
    """Evidence pointing at the layout handbook's paragraph holding ``fragment``."""
    layout = rig.sources["layout.md"]
    rows = rig.elements(layout)
    by_text = {row["text"]: row["id"] for row in rows}

    def cite(fragment: str) -> list[dict]:
        element = next(eid for text, eid in by_text.items() if fragment in text)
        return [{"source_id": layout, "source_title": "版图设计手册",
                 "element_id": element, "element_type": "paragraph",
                 "location_label": "p1", "quoted_span": fragment, "confidence": 1.0}]
    return cite


def library_world(rig: Rig) -> None:
    """The documents plus a small graph whose objects cite their passages."""
    documents_world(rig)
    layout = rig.sources["layout.md"]
    cite = _layout_citer(rig)
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


def chain_world(rig: Rig) -> None:
    """The documents plus a two-hop ``part_of`` chain whose relations cite their
    passages (what ``follow_chain`` composes)."""
    documents_world(rig)
    layout = rig.sources["layout.md"]
    cite = _layout_citer(rig)
    nodes = [
        _node("A", "版图设计", evidence=cite("版图设计要兼顾")),
        _node("B", "寄生电容", evidence=cite("耦合电容")),
        _node("F", "分层布线", evidence=cite("分层布线")),
    ]
    edges = [
        {**_edge("B", "A", "part_of"), "evidence": cite("耦合电容")},
        {**_edge("A", "F", "part_of"), "evidence": cite("分层布线")},
    ]
    lifecycle = rig.repo._runtime.knowledge_lifecycle
    lifecycle.store_kg(rig.notebook_id, layout, nodes, edges)
    lifecycle.rebuild_unified_kg(rig.notebook_id)


REPORT_TITLE = "版图与签核的要点"
REPORT_SECTIONS = ("寄生控制", "签核关口")
REPORT_ID = "rep-layout-1"


def report_world(rig: Rig) -> None:
    """The library plus the asker's finished deep report of it, which cites one
    layout passage and one sign-off paragraph (what a question can attach)."""
    library_world(rig)
    _attach_report(rig, "layout.md", "%耦合电容%")


PROCESS_MD = """# 工艺手册

## 退火

退火窗口决定晶格修复程度,温度过低修复不足。

退火之后要复测薄层电阻,确认激活率达标。
"""


def long_passage_report_world(rig: Rig) -> None:
    """The report world, except that the passage the report cites is a
    two-paragraph section of a third document."""
    library_world(rig)
    rig.upload("process.md", PROCESS_MD.encode(), "text/markdown")
    _attach_report(rig, "process.md", "%退火窗口%")


def _attach_report(rig: Rig, passage_source: str, passage_like: str) -> None:
    layout, signoff = rig.sources[passage_source], rig.sources["signoff.md"]
    (passage,) = rig.query("SELECT id FROM chunks WHERE source_id=? AND text LIKE ?",
                           (layout, passage_like))
    paragraph = next(row["id"] for row in rig.elements(signoff) if "DRC" in row["text"])
    references = [
        {"key": "k1", "object_type": "chunk", "object_id": passage["id"],
         "source_id": layout, "element_id": ""},
        {"key": "k2", "object_type": "element", "object_id": paragraph,
         "source_id": signoff, "element_id": paragraph},
    ]
    content = "\n".join([
        f"# {REPORT_TITLE}", "",
        "## 执行摘要", "寄生与签核是两道关口。", "",
        f"## {REPORT_SECTIONS[0]}", "长走线增加耦合电容 [k1]。", "",
        f"## {REPORT_SECTIONS[1]}", "DRC 与 LVS 违例不能流片 [k2]。", "",
        "## 参考文献", "[k1] 寄生段落",
    ])
    sections = json.dumps([{"title": title} for title in REPORT_SECTIONS], ensure_ascii=False)
    js = rig.json_placeholder
    rig.sql(
        "INSERT INTO reports (id,notebook_id,question,outline_json,sections_json,"
        "references_json,content_md,status,created_by,understanding_json,"
        f"created_at,updated_at) VALUES (?,?,?,{js},{js},{js},?,'done',?,{js},?,?)",
        (REPORT_ID, rig.notebook_id, REPORT_TITLE, sections, sections,
         json.dumps(references), content, rig.user_id, json.dumps({}), ASKED_AT, ASKED_AT),
    )


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
              understanding_ms: Optional[int] = 37,
              constraints: tuple = ("先进工艺",)) -> Callable[[Rig], AskRequest]:
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
            constraints=list(constraints),
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
    #: The world's uploaded sources by file name (and the peer notebook).
    sources: dict = dataclasses.field(default_factory=dict)

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
    # what the shipped draft stage (``_draft_reasoning_response``) calls
    "_reasoning_report_turn", "_assemble_structured_evidence", "_answer_reasoning",
    "_answer_reasoning_sections", "_answer_with_retry", "_memory_citations",
    "_reasoning_chunk_cards", "_extend_report_cards", "_external_exact_keys",
    "_append_spreadsheet_citations", "_append_external_citations",
    "_reasoning_result_sets",
)

#: The draft stage's collaborators on the service's ports: ``(label, attribute,
#: methods)``.
_PORT_METHODS = (
    ("evidence", "evidence_context", ("citations_from", "element_citations",
                                      "chunk_citations", "collection_item_citations",
                                      "external_citations", "attach_citation_images")),
    ("schemas", "schemas", ("effective_schemas",)),
    ("models", "model_clients", ("chat",)),
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
    ("app.services.outline_synthesis", ("plan_outline_sections",)),
)


def _install_recording(rig: Rig, monkeypatch: Any) -> None:
    import importlib

    recorder, hooks, service = rig.recorder, rig.hooks, rig.service
    for name in _SELF_METHODS:
        monkeypatch.setattr(service, name,
                            _recorded("self", name, getattr(service, name), recorder, hooks))
    for label, attribute, names in _PORT_METHODS:
        port = getattr(service, attribute)
        for name in names:
            monkeypatch.setattr(port, name,
                                _recorded(label, name, getattr(port, name), recorder, hooks))
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
        sources=dict(rig.sources),
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
#: runs first and embeds outside the run's embedding memo, finds no candidate
#: with a vector and so does not embed at all -- only the retrieval stage
#: embeds the question, once, inside the memo.
QUESTION_EMBEDS_PER_RUN = 1
#: ... and in ``memory_world``, where the asker owns a confirmed Memory with a
#: stored vector: that recall does embed the question (the skip must not fire
#: -- dropping this embed would zero every vector score of the Memory), and the
#: retrieval stage reads that native vector back
#: (``retrieval_run.shared_query_vector``) instead of embedding the same text
#: again, so the run still totals one. The scores this leaves byte-identical
#: are pinned in ``memory_embed_share_cases``.
QUESTION_EMBEDS_WITH_VECTOR_MEMORY = QUESTION_EMBEDS_PER_RUN


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
    # The asker's Memory has a stored vector, so the recall embeds the
    # research question -- the question carrying the confirmed contract -- and
    # the retrieval stage reuses that vector: every text, that one included,
    # is embedded exactly once.
    embedded = collections.Counter(obs.embedded())
    (question,) = [text for text in embedded if text.startswith(QUESTION)]
    assert embedded[question] == QUESTION_EMBEDS_WITH_VECTOR_MEMORY, embedded
    assert set(embedded.values()) == {1}, embedded


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
    # The workbook analysis's row locator is one of the answer's citations.
    workbook = obs.sources["timing.xlsx"]
    (sheet,) = [r for r in response.result_sets if type(r).__name__ == "SpreadsheetAnalysisResult"]
    receipt = next(row.citation for row in sheet.rows if row.citation)
    assert receipt.source_id == workbook and receipt.location_label
    assert [(c.element_id, c.location_label) for c in response.citations].count(
        (receipt.element_id, receipt.location_label)) == 1


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
    # The manifest the draft builds: the selected passages in the retrieval's
    # order, the trace as the draft found it, the synthesis's own assembly.
    assert [ref["dc:BaselineEvidenceRef"]["item_id"]
            for ref in manifest["selected_chunks"]] == historical
    assert manifest["baseline_step_usage"] == len(draft["trace"])
    prompt = manifest["prompt"]["dc:BaselinePromptAssembly"]
    assert prompt["budget_chars"] > 0 and prompt["citation_handles"]
    # The admitted outside item is cited.
    assert EXTERNAL_ITEM[6] in [c.url for c in response.citations]


def _record_graph_fail_closed(rig: Rig) -> None:
    """Note every fail-closed status the selected-source graph service records
    (it is what emits the content-free ``selected_source_graph`` event)."""
    graph = rig.service.selected_source_graph
    assert graph is not None, "the shipped wiring configures the graph lane"
    real = graph.fail_closed

    def fail_closed(notebook_id, baseline, reason, **kwargs):
        rig.recorder.note({"graph_fail_closed": reason,
                           "baseline": [chunk.chunk_id for chunk in baseline]})
        return real(notebook_id, baseline, reason, **kwargs)

    graph.fail_closed = fail_closed


def _activation_fails(rig: Rig) -> None:
    """Retrieval returns, then the selected-source graph lane blows up."""
    _snapshot_extras(rig)
    _record_graph_fail_closed(rig)

    def boom(real, *args, **kwargs):
        raise RuntimeError("selected-source graph lane broke")

    rig.hooks.replace["self._activate_selected_source_graph"] = boom


def _retrieval_snapshot(obs: Observation) -> dict:
    (call,) = obs.calls("fn.execute_reasoning_retrieval_stage")
    return call["ret"]["dc:ReasoningEvidenceSnapshot"]


def _chunk_ids(chunks: list) -> list[str]:
    return [chunk["dc:RetrievedChunk"]["chunk_id"] for chunk in chunks]


def _committed_manifest(obs: Observation) -> dict:
    (stage,) = obs.calls("self._run_reasoning_stage")
    committed = stage["ret"]["dc:CommittedReasoningAnswer"]
    return committed["baseline_manifest"]["dc:RetrievalBaselineManifest"]


def _ledger_ids(manifest: dict, key: str) -> list[str]:
    return [ref["dc:BaselineEvidenceRef"]["item_id"] for ref in manifest[key]]


def _assert_kept_retrieval_evidence(obs: Observation, *, synthesized: bool = True) -> dict:
    """The graph lane failed after retrieval returned: everything retrieval
    produced is the draft's input, only the lane's own append is missing, and
    the lane leaves no trace step (its state never enters the trace)."""
    snapshot, draft = _retrieval_snapshot(obs), _draft_input(obs)
    for key in ("top_hits", "elements", "chains", "enumerations", "document_reads",
                "outline", "outline_evidence"):
        assert draft[key] == snapshot[key], key
    assert draft["collection_map_text"] == snapshot["collection_map_text"]
    assert draft["external_evidence"] == snapshot["external_evidence"]
    assert _chunk_ids(draft["historical_chunks"]) == _chunk_ids(snapshot["chunks"])
    # The lane fails closed to the retrieval's passages, as a fresh copy.
    assert draft["chunks"] == draft["historical_chunks"]
    # The trace is the pre-retrieval steps plus the retrieval's, nothing added.
    assert draft["trace"][2:] == snapshot["trace"], [
        s["pd:TraceStep"]["step_type"] for s in draft["trace"]]
    # The response reports that trace, plus the synthesis step when one ran.
    drafted = [s["pd:TraceStep"]["step_type"] for s in draft["trace"]]
    assert obs.trace_kinds() == drafted + (["synthesis"] if synthesized else []), drafted
    # One fail-closed status (the reason code the event carries) over exactly
    # the passages the draft reads; the log names only the exception class.
    closed = [e for e in obs.timeline if "graph_fail_closed" in e]
    assert closed == [{"graph_fail_closed": "activation_seam_failed",
                       "baseline": _chunk_ids(draft["chunks"])}], closed
    assert {"logger": "silicon_notebook.ask_service", "level": "WARNING",
            "message": "selected-source graph lane failed (RuntimeError)"} in obs.logs
    return draft


def _expect_activation_fails(obs: Observation) -> None:
    # The lane only appends to what retrieval found, so its failure costs only
    # that append: the run answers from the retrieval's evidence.
    response = _answered(obs, llm_mode="grounded")
    _ran_stages(obs)
    draft = _assert_kept_retrieval_evidence(obs)
    passages = _chunk_ids(draft["chunks"])
    assert len(passages) > 1 and draft["top_hits"]
    assert "plan" in obs.trace_kinds()
    assert draft["candidate_manifest"] is not None
    assert [item["dc:ExternalEvidence"]["key"] for item in draft["external_evidence"]] == [
        EXTERNAL_ITEM[0]]
    # The ledger's selected items are what the draft was given.
    manifest = _committed_manifest(obs)
    assert _ledger_ids(manifest, "selected_chunks") == passages
    assert _ledger_ids(manifest, "selected_knowledge") == [
        hit["dc:RetrievedKnowledge"]["object_id"] for hit in draft["top_hits"]]
    # The answer is grounded in the retrieval's passages and cites them (and
    # the admitted outside item).
    assert response.grounded is True and "[k" in response.answer and response.anchors
    cited = {c.element_id for c in response.citations if c.element_id}
    passage_elements = {element_id for chunk in draft["chunks"]
                        for element_id in chunk["dc:RetrievedChunk"]["element_ids"]}
    assert cited & passage_elements, (cited, passage_elements)
    assert EXTERNAL_ITEM[6] in [c.url for c in response.citations]


def _expect_activation_fails_streamed(obs: Observation) -> None:
    _expect_activation_fails(obs)
    # Every step streamed while retrieval ran is still in the response.
    _streamed_as_traced(obs)


def _activation_fails_on_empty_retrieval(rig: Rig) -> None:
    """Retrieval returns, having found nothing; then the graph lane blows up."""
    _record_graph_fail_closed(rig)

    def empty(real, *args, **kwargs):
        result = real(*args, **kwargs)
        return dataclasses.replace(result, **{
            key: type(getattr(result, key))() for key in (
                "top_hits", "elements", "chunks", "chains", "enumerations",
                "document_reads", "collection_map_text", "outline",
                "outline_evidence", "external_evidence")})

    def boom(real, *args, **kwargs):
        raise RuntimeError("selected-source graph lane broke")

    rig.hooks.replace["fn.execute_reasoning_retrieval_stage"] = empty
    rig.hooks.replace["self._activate_selected_source_graph"] = boom


def _expect_activation_fails_on_empty_retrieval(obs: Observation) -> None:
    # Nothing was found and nothing is invented: the deterministic
    # no-evidence answer, with the retrieval's steps still reported.
    response = _answered(obs, llm_mode="deterministic")
    _streamed_as_traced(obs)
    draft = _assert_kept_retrieval_evidence(obs, synthesized=False)
    for key in ("top_hits", "chunks", "historical_chunks", "external_evidence"):
        assert draft[key] == [], key
    assert "plan" in obs.trace_kinds()
    assert response.answer == "" and response.grounded is False
    manifest = _committed_manifest(obs)
    assert manifest["selected_chunks"] == [] and manifest["selected_knowledge"] == []


def _wire_contributor_host(rig: Rig) -> None:
    """The process-shared contributor host the application bootstrap injects
    (a bare repository has none, so its graph lane returns before any work):
    with it the real lane runs as in production."""
    from app.extensions import default_extension_runtime

    rig.service.retrieval_contributors = default_extension_runtime().retrieval_contributors


def _cancel_during_activation(rig: Rig) -> None:
    """The user cancels while the selected-source graph lane runs (the real
    lane, which checks the cancellation inside the contributor host)."""
    _wire_contributor_host(rig)
    rig.hooks.before["self._activate_selected_source_graph"] = (
        lambda *args, **kwargs: rig.cancel.set())


def _expect_cancel_during_activation(obs: Observation) -> None:
    _raised(obs, "AskCancelled")
    (activate,) = obs.calls("self._activate_selected_source_graph")
    assert activate.get("raised", "").startswith("AskCancelled"), activate
    # The run ends there: no table lane, no draft, no answer.
    assert not _called(obs, "self._spreadsheet_reasoning_results")
    assert not _called(obs, "fn.execute_response_draft_stage")
    assert obs.models("ask_answer") == []


def _activation_refused(rig: Rig) -> None:
    """A retrieval-control refusal raised inside the real lane's own handler
    (where it builds the contributor envelope)."""
    from app.domain.retrieval_control import RetrievalControlError
    from app.services import ask_service as module

    _wire_contributor_host(rig)

    def activate(real, *args, **kwargs):
        original = module.selected_source_graph_call_context

        def refuse(*_args, **_kwargs):
            rig.recorder.note({"lane_refused": True})
            raise RetrievalControlError("participant override refused")

        module.selected_source_graph_call_context = refuse
        try:
            return real(*args, **kwargs)
        finally:
            module.selected_source_graph_call_context = original

    rig.hooks.replace["self._activate_selected_source_graph"] = activate


def _refuse_in_graph_service(rig: Rig) -> None:
    """A retrieval-control refusal raised by the graph service's own snapshot
    leg -- beneath the contributor host, which absorbs whatever a contribution
    raises -- with the real call envelope and the real host."""
    from types import SimpleNamespace

    from app.domain.retrieval_control import RetrievalControlError

    _wire_contributor_host(rig)

    def refuse(*_args, **_kwargs):
        rig.recorder.note({"lane_refused": True})
        raise RetrievalControlError("participant override refused")

    rig.service.selected_source_graph._snapshots = SimpleNamespace(snapshot=refuse)


def _narrowed_include(rig: Rig) -> AskRequest:
    """The library Ask with only the layout document ticked: the frozen
    include scope the graph service walks (an exclude scope stops before any
    graph read)."""
    from app.models.source_scope import SourceScope

    return AskRequest(question=QUESTION, mode="reasoning", asked_at=ASKED_AT,
                      source_scope=SourceScope(mode="include",
                                               source_ids=[rig.sources["layout.md"]]))


def _expect_graph_service_refused(obs: Observation) -> None:
    _expect_activation_refused(obs)
    (activate,) = obs.calls("self._activate_selected_source_graph")
    assert activate.get("raised", "").startswith("RetrievalControlError"), activate


def _graph_service_refused_in_report(rig: Rig, payload, job_id, cancel, on_trace):
    """The Deep Report twin: the real section pipeline (``_run_sections``:
    each section's deep dive -- retrieval, then the report's own
    selected-source graph lane -- behind the section's fail-soft handler)
    under a narrowed scope."""
    import dataclasses as _dc

    from app.services.report_engine import ReportEngine
    from app.services.retrieval_run import retrieval_run
    from app.services.source_scope import source_scope_context

    del payload, job_id, on_trace
    from app.extensions import default_extension_runtime

    engine = ReportEngine.from_repository(rig.repo, rig.service.settings, cancel)
    engine = ReportEngine(
        _dc.replace(engine.dependencies,
                    retrieval_contributors=default_extension_runtime().retrieval_contributors),
        user_id=engine.user_id, cancel_event=engine.cancel_event)
    section = {"title": REPORT_SECTIONS[0], "scope": "版图寄生",
               "sub_queries": ["寄生电容如何控制"], "intent_questions": []}
    with retrieval_run(run_kind="report_generation", actor_id=engine.user_id,
                       cancel_event=cancel), source_scope_context(
            rig.notebook_id, {"mode": "include", "narrowed": True,
                              "source_ids": [rig.sources["layout.md"]]}):
        return engine._run_sections(rig.notebook_id, "rep-graph-refused", [section],
                                    QUESTION, 2)


def _expect_report_graph_service_refused(obs: Observation) -> None:
    _raised(obs, "RetrievalControlError", "participant override refused")
    # Sections retrieve on the pipeline's worker threads.
    assert [e for e in obs.timeline + obs.worker if "lane_refused" in e] == [
        {"lane_refused": True}]


def _expect_activation_refused(obs: Observation) -> None:
    _raised(obs, "RetrievalControlError", "participant override refused")
    assert [e for e in obs.timeline if "lane_refused" in e] == [{"lane_refused": True}]
    assert not _called(obs, "self._spreadsheet_reasoning_results")
    assert not _called(obs, "fn.execute_response_draft_stage")


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

# ------------------------------------------- the response draft's own branches
#
# The scenarios above reach ``_draft_reasoning_response`` (the shipped draft
# stage) only on its everyday path. The ones below drive the branches it owns:
# the typed collection rows and their citations, the sampled document reads,
# element and relation anchors, the sectioned synthesis (written, partly
# grounded, fallen back, failed, fed a binding retrieval truncated away), an
# outline that does not reach two sections or meets a checklist, the attached
# report (and the outline it bypasses), the images of the cited evidence, the
# completeness disclosure the checklist may suppress, an answer client that is
# not configured, the projection cap and the dedupe of the related knowledge,
# a federated hit, a Stop at each of the draft's own checks, and the draft
# called directly. A few doctor the draft's input on its way in
# (``fn.execute_response_draft_stage``) where retrieval cannot be steered into
# the shape the branch needs.

_CANDIDATE_ID = re.compile(r"\(id=([^)]+)\)")


def _candidates(prompt: str) -> list[str]:
    body = prompt.split("Candidates so far:", 1)[-1]
    return list(dict.fromkeys(_CANDIDATE_ID.findall(body)))


def _outline(*titles: str, empty: tuple = ()) -> Callable[[str], dict]:
    """A final outline binding the first candidates one per section, plus
    sections nothing was found for."""
    def reflect(prompt: str) -> dict:
        ids = _candidates(prompt)
        sections = [{"id": f"s{index + 1}", "title": title, "evidence": [ids[index]]}
                    for index, title in enumerate(titles)]
        sections += [{"id": f"e{index + 1}", "title": title, "evidence": []}
                     for index, title in enumerate(empty)]
        return {"next_action": "update_outline", "reason": "整理", "sufficient": True,
                "outline_sections": sections}
    return reflect


def _outline_kinds(*prefixes: str) -> Callable[[str], dict]:
    """A final outline whose section ``i`` binds the first candidate whose id
    starts with ``prefixes[i]`` (a knowledge object, a passage)."""
    def reflect(prompt: str) -> dict:
        ids = _candidates(prompt)
        sections = [{"id": f"s{index + 1}", "title": title,
                     "evidence": [next(i for i in ids if i.startswith(prefix))]}
                    for index, (title, prefix) in enumerate(zip(SECTIONS, prefixes))]
        return {"next_action": "update_outline", "reason": "整理", "sufficient": True,
                "outline_sections": sections}
    return reflect


def _alarm_before_draft(rig: Rig) -> None:
    """A retrieval-side model alarm lands in the request's sink before the draft."""
    rig.hooks.before["self._spreadsheet_reasoning_results"] = (
        lambda *a, **k: rig.repo._runtime.models.note_model_error(
            "reflect", RuntimeError("reflect down"), workload_id="reasoning_agent"))


def _chain_from(name: str) -> Callable[[str], dict]:
    """``follow_chain`` from the candidate knowledge object called ``name``."""
    def reflect(prompt: str) -> dict:
        body = prompt.split("Candidates so far:", 1)[-1]
        (start,) = [found for line in body.splitlines() if f"] {name} (" in line
                    for found in re.findall(r"\(id=(ko-[^)]+)\)", line)]
        return {"next_action": "follow_chain", "reason": "顺链",
                "follow_chain": {"start_object_id": start, "direction": "both"}}
    return reflect


ENUMERATE_CLAIMS = {"next_action": "enumerate_kg_objects", "reason": "列出论断",
                    "enumerate": {"object_type": "claim"}}
LIST_SOURCES = {"next_action": "enumerate_elements", "reason": "看目录",
                "enumerate": {"collection": "sources"}}
READ_LAYOUT = {"next_action": "read_document", "reason": "读原文",
               "read_document": {"source": "layout.md"}}
SEARCH_ELEMENTS = {"next_action": "search_elements", "reason": "查元素",
                   "query": "DRC 物理验证"}
SECTIONS = ("甲部分", "乙部分")
EMPTY_SECTION = "丙部分"
#: Every evidence key of a prompt (``k12: ...``) is citable.
ANY_KEY = r"k\d+"


def _exhaustive(question: str = QUESTION, **fields) -> Callable[[Rig], AskRequest]:
    return plain(question, retrieval_effort="exhaustive", **fields)


def _doctored(change: Callable[[Rig, Any], Any]) -> Callable[[Rig], None]:
    """Hand the draft stage ``change(rig, draft_input)`` instead of the input."""
    def install(rig: Rig) -> None:
        def stage(real, implementation, draft_input, runtime):
            return real(implementation, change(rig, draft_input), runtime)
        rig.hooks.replace["fn.execute_response_draft_stage"] = stage
    return install


class _UnconfiguredChat:
    configured = False
    model = ""

    def chat_json(self, messages, schema_hint=None, **kwargs):
        raise AssertionError("an unconfigured answer client is never called")


def _answer_unconfigured(rig: Rig) -> None:
    """Only the answer workload is unbound; nothing else is (so no short circuit)."""
    bind_chat_client(rig.repo, "ask_answer", _UnconfiguredChat())


def _images_everywhere(rig: Rig) -> None:
    """Every element the answer asks images for has one."""
    from app.models.ask import CitationImage

    def images(element_ids):
        return {eid: CitationImage(element_id=eid, asset_id=f"img-{eid}")
                for eid in dict.fromkeys(eid for eid in element_ids if eid)}

    rig.service.evidence_context.citation_images_for = images


def _move_bound_hit_to_outline_evidence(rig: Rig, draft: Any) -> Any:
    """Both sections' bindings were cut from the final selection by top_n and
    ride on ``outline_evidence`` (what the retriever does then)."""
    bound = {key for section in draft.outline for key in section.evidence_keys}
    moved = [hit for hit in draft.top_hits if hit.object_id in bound]
    assert len(moved) == 2, bound
    return dataclasses.replace(
        draft, top_hits=tuple(h for h in draft.top_hits if h not in moved),
        outline_evidence=tuple(draft.outline_evidence) + tuple(moved))


def _duplicate_first_hit(rig: Rig, draft: Any) -> Any:
    first, *rest = draft.top_hits
    return dataclasses.replace(draft, top_hits=(first, first, *rest))


PEER_TYPE = "layout_rule"
#: The peer notebook's own field order for ``PEER_TYPE`` (the payload's is
#: ``name``, ``section_path``).
PEER_FIELDS = ("section_path", "name")


def peer_world(rig: Rig) -> None:
    """The library plus a second notebook a federated hit can come from, which
    defines an object type of its own with its own field order."""
    from app.models.knowledge import ObjectSchemaCreate

    library_world(rig)
    peer = rig.repo._runtime.catalog.create_notebook(NotebookCreate(name="参考库")).id
    rig.sources["peer"] = peer
    rig.service.schemas.create_notebook_object_schema(
        peer, ObjectSchemaCreate(object_type=PEER_TYPE, fields=list(PEER_FIELDS),
                                 primary="name"), created_by=rig.user_id)


def _first_hit_from_peer(rig: Rig, draft: Any) -> Any:
    first, *rest = draft.top_hits
    foreign = dataclasses.replace(first, notebook_id=rig.sources["peer"], object_type=PEER_TYPE)
    return dataclasses.replace(draft, top_hits=(foreign, *rest))


def _drop_reported_passage(rig: Rig, draft: Any) -> Any:
    """The turn's own retrieval did not find the passage the report cites, so
    only the report's evidence partition carries it."""
    (passage,) = rig.query("SELECT id FROM chunks WHERE text LIKE ?", ("%退火窗口%",))
    keep = tuple(chunk for chunk in draft.chunks if chunk.chunk_id != passage["id"])
    return dataclasses.replace(draft, chunks=keep)


ENUMERATE_CONCEPTS = {"next_action": "enumerate_kg_objects", "reason": "列出概念",
                      "enumerate": {"object_type": "concept"}}
ENUMERATE_FORMULAS = {"next_action": "enumerate_kg_objects", "reason": "列出公式",
                      "enumerate": {"object_type": "formula"}}
#: Above every ranked relevance this world produces: only a deterministic
#: (exact) key or a perfect score could ground an answer.
STRICT_TAU = {"EVIDENCE_TAU_HIGH": "0.99"}


def _report_request(**fields) -> Callable[[Rig], AskRequest]:
    return plain(report_id=REPORT_ID, **fields)


def _direct_draft(rig: Rig, payload, job_id, cancel, on_trace):
    """The shipped draft stage's body called directly (it is callable on its
    own): its two type gates, then an empty input outside any Ask -- no
    request-scoped model-error sink, no trace."""
    from app.application.ask_reasoning import ReasoningRetrievalRuntime, ResponseDraftInput

    service = rig.service
    runtime = ReasoningRetrievalRuntime(
        scope=None, retrieval_run=None, cancellation=cancel, trace_sink=on_trace,
        connection_probe=service.retrieval_connection_probe,
    )
    prepared = service._prepare_reasoning_ask(
        rig.notebook_id, payload, user_id=rig.user_id, job_id=job_id, runtime=runtime)
    contract = QueryIntentContract(objective=QUESTION, resolved_question=QUESTION)
    stage = ResponseDraftInput(
        prepared=prepared, intent_contract=contract, top_hits=(),
        elements=(), trace=(), chunks=(), chains=(), enumerations=(),
        collection_map_text="", outline=(), outline_evidence=(), historical_chunks=(),
        memory_hits=(), structured_batch=None, completeness_unavailable=False,
        kg_required=False, candidate_manifest=None,
    )
    outcomes = {}
    for label, args in (("stage", ("not a stage", runtime)),
                        ("runtime", (stage, "not a runtime"))):
        try:
            service._draft_reasoning_response(*args)
        except Exception as exc:  # noqa: BLE001 — the raise IS the observation
            outcomes[label] = f"{type(exc).__name__}: {exc}"
    outcomes["draft"] = service._draft_reasoning_response(stage, runtime)
    return outcomes


def _synthesis_detail(response: Any) -> dict:
    """The terminal synthesis step's detail (section progress steps have no
    ``anchors``)."""
    (step,) = [step for step in response.reasoning_trace or []
               if step.step_type == "synthesis" and "anchors" in (step.detail or {})]
    return step.detail


def _answer_prompts(obs: Observation) -> list[str]:
    return [call["messages"][-1]["content"] for call in obs.models("ask_answer")]


def _drafted(obs: Observation) -> dict:
    """What the shipped draft stage was handed (after any doctoring)."""
    (call,) = obs.calls("self._draft_reasoning_response")
    return call["args"][0]["dc:ResponseDraftInput"]


def _identity(citation: Any) -> tuple:
    return (citation.source_id, citation.element_id, citation.label)


def _expect_enumeration_rows_cited(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    _ran_stages(obs)
    (anchor,) = response.anchors
    assert (anchor.key, anchor.object_type) == ("k5001", "claim")
    (result,) = response.result_sets
    assert result.collection == "kg_objects" and result.coverage.complete
    assert result.coverage.returned_total == 1
    # The row's citation mirrors the bound k5001 anchor, once, at the tail.
    (mapping,) = obs.calls("evidence.collection_item_citations")
    (row_citation,) = mapping["ret"].values()
    row = row_citation["pd:Citation"]
    assert _identity(response.citations[-1]) == (row["source_id"], row["element_id"],
                                                 row["label"])
    assert [_identity(c) for c in response.citations].count(_identity(response.citations[-1])) == 1
    detail = _synthesis_detail(response)
    assert detail["enumerated_collections"] == 1 and detail["included_collections"] == 1
    assert detail["evidence_level"] == response.evidence_level == "grounded"
    assert detail["citations"] == len(response.citations)
    assert response.top_relevance > 0


def _expect_enumeration_uncited(obs: Observation) -> None:
    response = _answered(obs, llm_mode="ungrounded")
    # A checklist answer with no bound marker lists no ranked evidence as its
    # sources, although retrieval found some.
    assert response.anchors == [] and response.citations == []
    (found,) = obs.calls("evidence.citations_from")
    assert found["ret"]
    assert len(response.result_sets) == 1
    assert response.evidence_level == "inferred"
    assert _synthesis_detail(response)["citations"] == 0


def _expect_checklist_notice(suppressed: bool) -> Callable[[Observation], None]:
    def expect(obs: Observation) -> None:
        response = _answered(obs, llm_mode="grounded")
        assert _draft_input(obs)["completeness_unavailable"] is True
        (result,) = response.result_sets
        assert result.coverage.complete and result.coverage.returned_total == 1
        if suppressed:
            assert response.completeness_notice == ""
            assert UNVERIFIED not in response.answer + response.conclusion
        else:
            assert response.completeness_notice == UNVERIFIED
            assert response.conclusion.endswith(f"\n\n{UNVERIFIED}")
            assert response.answer.endswith(f"\n\n> {UNVERIFIED}")
    return expect


def _expect_document_read_cited(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    _ran_stages(obs)
    _streamed_as_traced(obs)
    assert [(a.key, a.object_type) for a in response.anchors] == [
        ("k7001", "element"), ("k7002", "element"), ("k7003", "element")]
    detail = _synthesis_detail(response)
    assert detail["included_document_reads"] == 5
    assert detail["citations"] == len(response.citations)
    bound = {a.object_id for a in response.anchors}
    assert bound <= {c.element_id for c in response.citations}
    assert obs.stream[-1]["summary"] == "已生成答案，引用 3 处证据"


def _expect_elements_cited(obs: Observation) -> None:
    # Under a strict tau the elements' own retrieval scores decide: overview.
    response = _answered(obs, llm_mode="ungrounded")
    assert [a.object_type for a in response.anchors] == ["element"] * 3
    (element_cards,) = obs.calls("evidence.element_citations")
    cards = [c["pd:Citation"]["element_id"] for c in element_cards["ret"]]
    assert cards and cards == [a.object_id for a in response.anchors]
    assert set(cards) <= {c.element_id for c in response.citations}
    assert _synthesis_detail(response)["included_elements"] == 6
    assert response.evidence_level == "overview", response.evidence_level


def _expect_chain_grounded(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    assert [(a.key, a.object_type) for a in response.anchors] == [
        ("k2001", "relation"), ("k2002", "relation")]
    # Only the relations are cited: their chain relevance is what grounds it.
    assert response.evidence_level == "grounded"


def _expect_chain_cited(obs: Observation) -> None:
    response = _answered(obs, llm_mode="ungrounded")
    assert [(a.key, a.object_type) for a in response.anchors] == [
        ("k2001", "relation"), ("k2002", "relation")]
    # The relations are in the evidence pool (cited, so not inferred) with
    # their chain's relevance, not a perfect one: under a strict tau, overview.
    assert response.evidence_level == "overview"
    assert _draft_input(obs)["chains"]


def _expect_memory_cited(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    (anchor,) = response.anchors
    assert anchor.object_type == "memory"
    (card,) = [c for c in response.citations if c.memory_id]
    assert card.memory_id == anchor.object_id and card.label == "Memory · 版图评审结论"


def _expect_sectioned(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    _ran_stages(obs)
    _streamed_as_traced(obs)
    assert response.answer.startswith(f"## {SECTIONS[0]}\n\n")
    assert f"\n\n## {SECTIONS[1]}\n\n" in response.answer
    assert [a.key for a in response.anchors] == ["k1", "k10001"]
    prompts = _answer_prompts(obs)
    assert [f"is titled: {title}" in prompt for prompt, title in zip(prompts, SECTIONS)] == [
        True, True] and len(prompts) == 2
    summaries = [step["summary"] for step in obs.stream]
    assert summaries[-3:] == ["已写完第 1/共 2 节", "已写完第 2/共 2 节",
                              "已生成答案，引用 2 处证据"]
    detail = _synthesis_detail(response)
    assert (detail["outline_sections"], detail["outline_fallback"]) == (2, False)
    assert detail["outline_skipped"] == [EMPTY_SECTION]
    assert detail["ungrounded_sections"] == [] and "outline_bypassed" not in detail
    assert response.evidence_level == "grounded"
    # The sections' own assembly counts are the answer's.
    assert (detail["included_kg"], detail["included_chunks"]) == (2, 0)
    # The planner keeps the configured exact-lookup reserve.
    from app.core.config import Settings

    (plan,) = obs.calls("fn.plan_outline_sections")
    assert plan["kwargs"]["exact_reserve"] == Settings.model_fields[
        "reasoning_exact_reserve"].default
    # Every section reads through the run's one citation-lookup memo.
    (sections,) = obs.calls("self._answer_reasoning_sections")
    assert "citation_lookups" in sections["kwargs"]


def _expect_sectioned_passage(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    assert response.answer.startswith(f"## {SECTIONS[0]}")
    draft = _drafted(obs)
    (bound,) = draft["outline"][1]["dc:OutlineSection"]["evidence_keys"]
    (chunk,) = [c["dc:RetrievedChunk"] for c in draft["chunks"]
                if c["dc:RetrievedChunk"]["chunk_id"] == bound]
    # The passage the second section bound gets its card: the sections'
    # assemblies are the baseline the passage cards are drawn from.
    (cards,) = obs.calls("self._reasoning_chunk_cards")
    assert cards["ret"], cards
    assert set(chunk["element_ids"]) & {c.element_id for c in response.citations}


def _expect_fallback_keeps_earlier_alarm(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    assert _synthesis_detail(response)["outline_fallback"] is True
    # Only the sectioned attempt's alarms are absorbed by the fallback; one
    # noted before the draft stays.
    assert [(e.stage, e.workload_id) for e in response.model_errors] == [
        ("model_call", "reasoning_agent")]


def _expect_narrowed_without_ledger(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    draft = _drafted(obs)
    assert draft["candidate_manifest"] is None
    (stage,) = obs.calls("self._run_reasoning_stage")
    manifest = stage["ret"]["dc:CommittedReasoningAnswer"]["baseline_manifest"][
        "dc:RetrievalBaselineManifest"]
    # Without the retriever's ledger the candidates are what the draft was given.
    ids = [ref["dc:BaselineEvidenceRef"]["item_id"] for ref in manifest["candidate_knowledge"]]
    assert ids and ids == [h["dc:RetrievedKnowledge"]["object_id"] for h in draft["top_hits"]]
    chunks = [ref["dc:BaselineEvidenceRef"]["item_id"] for ref in manifest["candidate_chunks"]]
    assert chunks and chunks == [c["dc:RetrievedChunk"]["chunk_id"] for c in draft["chunks"]]
    assert response.answer


def _expect_sectioned_partly_grounded(obs: Observation) -> None:
    response = _answered(obs, llm_mode="ungrounded")
    # One section without support caps the whole answer at overview.
    assert response.evidence_level == "overview" and response.grounded is False
    detail = _synthesis_detail(response)
    assert detail["ungrounded_sections"] == [SECTIONS[1]]
    assert detail["evidence_level"] == "overview"


def _expect_sectioned_falls_back(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    _streamed_as_traced(obs)
    prompts = _answer_prompts(obs)
    assert len(prompts) == 4 and "is titled" not in prompts[-1]
    assert "## " not in response.answer and response.anchors
    detail = _synthesis_detail(response)
    assert (detail["outline_sections"], detail["outline_fallback"]) == (0, True)
    # The second section's failure was absorbed by the fallback: no alarm.
    assert response.model_errors == []
    assert [step["summary"] for step in obs.stream][-2:] == [
        "已写完第 1/共 2 节", "已生成答案，引用 3 处证据"]


def _expect_sectioned_and_fallback_fail(obs: Observation) -> None:
    response = _answered(obs, llm_mode="synthesis_failed")
    assert len(_answer_prompts(obs)) == 4
    # Both the section's alarms and the fallback's stay: nothing was absorbed.
    assert [(e.stage, e.workload_id) for e in response.model_errors] == [
        ("answer", "ask_answer")] * 6
    detail = _synthesis_detail(response)
    assert detail["outline_fallback"] is True
    hits = len(_draft_input(obs)["top_hits"])
    assert response.conclusion == (
        f"已检索到 {hits} 条相关证据,但本次答案合成未产出内容。请重试该问题;下方为已检索到的证据。")


def _expect_truncated_binding(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    draft = _drafted(obs)
    moved = {hit["dc:RetrievedKnowledge"]["object_id"] for hit in draft["outline_evidence"]}
    assert moved and moved <= {a.object_id for a in response.anchors}
    assert not moved & {item.id for item in response.related_knowledge}
    # The bindings the selection lost are in the pool, so their anchors ground.
    assert moved == {a.object_id for a in response.anchors}
    assert response.evidence_level == "grounded"


def _expect_outline_single_section(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    (prompt,) = _answer_prompts(obs)
    assert "is titled" not in prompt and "## " not in response.answer
    detail = _synthesis_detail(response)
    assert (detail["outline_sections"], detail["outline_fallback"]) == (0, False)
    assert detail["outline_skipped"] == [EMPTY_SECTION] and "outline_bypassed" not in detail


def _expect_outline_with_checklist(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    (prompt,) = _answer_prompts(obs)
    assert "is titled" not in prompt
    detail = _synthesis_detail(response)
    assert detail["outline_sections"] == 0 and detail["outline_skipped"] == []
    assert "outline_bypassed" not in detail and detail["enumerated_collections"] == 1
    assert response.anchors[0].key == "k5001" and len(response.result_sets) == 1


def _expect_report_attached(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    _streamed_as_traced(obs)
    assert response.linked_report.status == "read"
    assert response.linked_report.evidence_restored == 2
    assert [(a.key, a.object_type) for a in response.anchors] == [
        ("k1", "chunk"), ("k4001", "element")]
    (prompt,) = _answer_prompts(obs)
    assert "Attached report the user is asking about" in prompt and REPORT_TITLE in prompt
    detail = _synthesis_detail(response)
    assert detail["included_report_evidence"] == 1
    assert response.evidence_level == "grounded"
    assert "report" in obs.trace_kinds()


def _expect_report_bypasses_outline(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    (prompt,) = _answer_prompts(obs)
    assert "is titled" not in prompt
    detail = _synthesis_detail(response)
    assert detail["outline_bypassed"] == "linked_report"
    assert detail["outline_sections"] == 0
    assert response.linked_report.status == "read"


def _expect_images(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    assert [a.key for a in response.anchors] == ["k5001", "k1", "k4001"]
    assert all(len(a.images) >= 1 for a in response.anchors)
    (mapping,) = obs.calls("evidence.collection_item_citations")
    (row,) = mapping["ret"].values()
    row_identity = (row["pd:Citation"]["source_id"], row["pd:Citation"]["element_id"],
                    row["pd:Citation"]["label"])
    (cards,) = obs.calls("self._reasoning_chunk_cards")
    card_identities = {(c["pd:Citation"]["source_id"], c["pd:Citation"]["element_id"],
                        c["pd:Citation"]["label"]) for c in cards["ret"]}
    assert card_identities
    for citation in response.citations:
        identity = _identity(citation)
        if identity == row_identity or identity in card_identities:
            # A checklist row's citation and a passage card bring no image.
            assert citation.images == [], identity
        else:
            assert citation.images, identity
    total = sum(len(a.images) for a in response.anchors) + sum(
        len(c.images) for c in response.citations)
    assert 0 < total <= 12


def _expect_answer_unconfigured(obs: Observation) -> None:
    response = _answered(obs, llm_mode="deterministic")
    _streamed_as_traced(obs)
    from app.services.ask_service import _NO_RETRIEVAL_EVIDENCE_MESSAGE

    assert response.conclusion == _NO_RETRIEVAL_EVIDENCE_MESSAGE and response.answer == ""
    assert obs.trace_kinds()[-1] == "answer" and obs.models("ask_answer") == []
    assert response.related_knowledge and response.citations
    (structured,) = obs.calls("self._assemble_structured_evidence")
    assert structured["args"][2] == ""  # no collection map block without a client


def _expect_hybrid_answer_unconfigured(obs: Observation) -> None:
    response = _answered(obs, llm_mode="deterministic")
    line = "Knowhow 枚举：3/3 行，完整。 分析未运行。"
    assert response.answer == line
    assert response.conclusion.startswith(f"{line}\n\n")
    assert response.result_coverage.complete is True and response.result_sets


def _expect_synthesis_failed_without_hits(obs: Observation) -> None:
    response = _answered(obs, llm_mode="synthesis_failed", kg_required=True)
    assert response.conclusion == "本次答案合成未产出内容,请重试该问题。"
    assert response.related_knowledge == [] and response.citations
    assert [(e.stage, e.workload_id) for e in response.model_errors] == [
        ("answer", "ask_answer")] * 3


def _expect_related_capped(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    hits = [hit["dc:RetrievedKnowledge"]["object_id"] for hit in _drafted(obs)["top_hits"]]
    distinct = list(dict.fromkeys(hits))
    assert len(hits) > len(distinct) > 2
    assert [item.id for item in response.related_knowledge] == distinct[:2]


def _expect_federated(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    hits = [hit["dc:RetrievedKnowledge"] for hit in _drafted(obs)["top_hits"]]
    peer = hits[0]["notebook_id"]
    assert peer and peer != obs.notebook_id
    # The foreign hit is laid out by its own notebook's definition of its type.
    first = response.related_knowledge[0]
    assert first.object_type == PEER_TYPE
    assert [field.key for field in first.fields] == list(PEER_FIELDS)
    # Each hit is rendered against its own notebook's schema overlay.
    asked = [call["args"][0] for call in obs.calls("schemas.effective_schemas")]
    assert sorted(asked) == sorted({obs.notebook_id, peer}), asked
    assert len(response.related_knowledge) == len(hits)


def _expect_report_alone(obs: Observation) -> None:
    # Retrieval failed soft: the attached report's evidence is all there is,
    # and it alone is reason enough to write an answer.
    response = _answered(obs, llm_mode="grounded")
    draft = _draft_input(obs)
    for key in ("top_hits", "elements", "chunks", "chains", "enumerations"):
        assert draft[key] == [], key
    assert draft["collection_map_text"] == "" and draft["memory_hits"] == []
    assert len(_answer_prompts(obs)) == 1 and response.anchors
    assert response.linked_report.status == "read"
    assert _synthesis_detail(response)["included_report_evidence"] == 2


def _expect_rows_share_a_passage(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    rows = [a for a in response.anchors if a.key.startswith("k5")]
    assert len(rows) == 3
    (mapping,) = obs.calls("evidence.collection_item_citations")
    row_identities = {(c["pd:Citation"]["source_id"], c["pd:Citation"]["element_id"],
                       c["pd:Citation"]["label"]) for c in mapping["ret"].values()}
    assert len(row_identities) < len(rows)  # two rows cite the same paragraph
    cited = [_identity(c) for c in response.citations]
    for identity in row_identities:
        assert cited.count(identity) <= 1, identity


def _expect_empty_checklist_keeps_notice(obs: Observation) -> None:
    response = _answered(obs, llm_mode="ungrounded")
    assert _draft_input(obs)["completeness_unavailable"] is True
    (result,) = response.result_sets
    assert result.coverage.complete and result.coverage.returned_total == 0
    # A complete list of nothing does not vouch for "all the results".
    assert response.completeness_notice == UNVERIFIED


def _expect_report_passage_images(obs: Observation) -> None:
    response = _answered(obs, llm_mode="grounded")
    # The anchor is the passage only the report's evidence partition carries:
    # it still brings an image for each of its elements.
    (passage,) = response.anchors
    assert passage.object_type == "chunk"
    (cards,) = obs.calls("self._extend_report_cards")
    _citations, report_chunks, _keys = cards["ret"]  # a ReportCards named tuple
    (restored,) = report_chunks
    elements = set(restored["dc:RetrievedChunk"]["element_ids"])
    assert len(elements) > 1
    assert {image.element_id for image in passage.images} == elements


def _expect_cancel_first_check(obs: Observation) -> None:
    _raised(obs, "AskCancelled")
    assert _called(obs, "self._draft_reasoning_response")
    assert not _called(obs, "evidence.citations_from")
    assert obs.models("ask_answer") == []


def _expect_cancel_before_model(obs: Observation) -> None:
    _raised(obs, "AskCancelled")
    assert _called(obs, "evidence.citations_from")
    assert not _called(obs, "self._assemble_structured_evidence")
    assert "ask_answer" not in [call["args"][0] for call in obs.calls("models.chat")]


def _expect_cancel_between_sections(obs: Observation) -> None:
    _raised(obs, "AskCancelled")
    # The finished section's step is checked before it is recorded or streamed.
    assert len(obs.models("ask_answer")) == 1
    assert not [step for step in obs.stream if step["summary"].startswith("已写完")]


def _expect_draft_called_directly(obs: Observation) -> None:
    from app.services.ask_service import _NO_RETRIEVAL_EVIDENCE_MESSAGE

    outcomes = obs.response
    assert outcomes["stage"] == "StageBoundaryError: invalid reasoning response draft input"
    assert outcomes["runtime"] == "StageBoundaryError: invalid reasoning response draft runtime"
    response = outcomes["draft"].response
    assert (response.llm_mode, response.mode) == ("deterministic", "reasoning")
    assert response.conclusion == _NO_RETRIEVAL_EVIDENCE_MESSAGE
    assert response.reasoning_trace is None and response.model_errors == []
    assert response.related_knowledge == [] and response.citations == []
    assert outcomes["draft"].asked_at == ASKED_AT


DRAFT_SCENARIOS = [
    Scenario("enumeration_rows_cited", library_world, plain(), _expect_enumeration_rows_cited,
             script=Script(cite=r"k5\d{3}", reflects=(ENUMERATE_CLAIMS,))),
    Scenario("enumeration_uncited", library_world, plain(), _expect_enumeration_uncited,
             script=Script(cite="none", reflects=(ENUMERATE_CLAIMS,))),
    Scenario("enumeration_suppresses_notice", library_world,
             confirmed(LIST_ALL, topics=(), result_scope="complete", completeness=True,
                       constraints=()),
             _expect_checklist_notice(suppressed=True), script=Script(cite=r"k5\d{3}", reflects=(ENUMERATE_CLAIMS,))),
    Scenario("enumeration_predicate_keeps_notice", library_world,
             confirmed(LIST_ALL, topics=(), result_scope="complete", completeness=True),
             _expect_checklist_notice(suppressed=False), script=Script(cite=r"k5\d{3}", reflects=(ENUMERATE_CLAIMS,))),
    Scenario("enumeration_aggregate_keeps_notice", library_world,
             confirmed(COUNT_ALL, topics=(), result_scope="aggregate", completeness=True,
                       constraints=()),
             _expect_checklist_notice(suppressed=False), script=Script(cite=r"k5\d{3}", reflects=(ENUMERATE_CLAIMS,))),
    Scenario("document_read_cited", library_world, plain(), _expect_document_read_cited, stream=True,
             script=Script(cite=r"k7\d{3}", reflects=(LIST_SOURCES, READ_LAYOUT))),
    Scenario("elements_cited", library_world, plain(), _expect_elements_cited,
             env=STRICT_TAU, script=Script(cite=r"k4\d{3}", reflects=(SEARCH_ELEMENTS,))),
    Scenario("chain_grounded", chain_world, plain(), _expect_chain_grounded,
             script=Script(cite=r"k2\d{3}", reflects=(_chain_from("寄生电容"),))),
    Scenario("chain_cited", chain_world, plain(), _expect_chain_cited, env=STRICT_TAU,
             script=Script(cite=r"k2\d{3}", reflects=(_chain_from("寄生电容"),))),
    Scenario("enumeration_rows_share_a_passage", library_world, plain(),
             _expect_rows_share_a_passage,
             script=Script(cite=r"k5\d{3}", reflects=(ENUMERATE_CONCEPTS,))),
    Scenario("enumeration_empty_keeps_notice", library_world,
             confirmed(LIST_ALL, topics=(), result_scope="complete", completeness=True,
                       constraints=()),
             _expect_empty_checklist_keeps_notice,
             script=Script(cite="none", reflects=(ENUMERATE_FORMULAS,))),
    Scenario("report_alone_triggers_synthesis", report_world, _report_request(),
             _expect_report_alone, install=_retrieval_raises(_runtime_error),
             script=Script(cite=ANY_KEY)),
    Scenario("report_passage_images", long_passage_report_world, _report_request(),
             _expect_report_passage_images,
             install=lambda rig: (_images_everywhere(rig),
                                  _doctored(_drop_reported_passage)(rig)),
             script=Script(cite=r"k\d{1,3}",
                           cite_after="[Evidence the attached report cited]")),
    Scenario("sectioned_answer", library_world, _exhaustive(), _expect_sectioned, stream=True,
             script=Script(cite=ANY_KEY,
                           reflects=(_outline(*SECTIONS, empty=(EMPTY_SECTION,)),))),
    Scenario("sectioned_binds_a_passage", library_world, _exhaustive(),
             _expect_sectioned_passage,
             script=Script(cite=ANY_KEY, reflects=(_outline_kinds("ko-", "ck-"),))),
    Scenario("sectioned_fallback_keeps_earlier_alarm", library_world, _exhaustive(),
             _expect_fallback_keeps_earlier_alarm, install=_alarm_before_draft,
             script=Script(cite=ANY_KEY, answer_fails_on=("is titled: 乙部分",),
                           reflects=(_outline(*SECTIONS),))),
    Scenario("memory_cited", memory_world, plain(), _expect_memory_cited,
             script=Script(cite=r"k3\d{3}")),
    Scenario("narrowed_without_candidate_ledger", library_world, _narrowed,
             _expect_narrowed_without_ledger,
             install=_doctored(lambda rig, draft: dataclasses.replace(
                 draft, candidate_manifest=None))),
    Scenario("sectioned_partly_grounded", library_world, _exhaustive(), _expect_sectioned_partly_grounded,
             script=Script(cite=ANY_KEY, uncited_on=("is titled: 乙部分",),
                           reflects=(_outline(*SECTIONS),))),
    Scenario("sectioned_falls_back", library_world, _exhaustive(), _expect_sectioned_falls_back, stream=True,
             script=Script(cite=ANY_KEY, answer_fails_on=("is titled: 乙部分",),
                           reflects=(_outline(*SECTIONS),))),
    Scenario("sectioned_and_fallback_fail", library_world, _exhaustive(), _expect_sectioned_and_fallback_fail,
             script=Script(cite=ANY_KEY, reflects=(_outline(*SECTIONS),),
                           failures={"ask_answer": RuntimeError("answer model down")})),
    Scenario("sectioned_truncated_binding", library_world, _exhaustive(), _expect_truncated_binding,
             install=_doctored(_move_bound_hit_to_outline_evidence),
             script=Script(cite=ANY_KEY, reflects=(_outline(*SECTIONS),))),
    Scenario("outline_single_section", library_world, _exhaustive(), _expect_outline_single_section,
             script=Script(cite=ANY_KEY,
                           reflects=(_outline(SECTIONS[0], empty=(EMPTY_SECTION,)),))),
    Scenario("outline_with_checklist", library_world, _exhaustive(), _expect_outline_with_checklist,
             script=Script(cite=ANY_KEY, reflects=(ENUMERATE_CLAIMS, _outline(*SECTIONS)))),
    Scenario("report_attached", report_world, _report_request(), _expect_report_attached, stream=True,
             script=Script(cite=r"k1|k4\d{3}")),
    Scenario("report_bypasses_outline", report_world,
             _report_request(retrieval_effort="exhaustive"), _expect_report_bypasses_outline,
             script=Script(cite=ANY_KEY, reflects=(_outline(*SECTIONS),))),
    Scenario("images_attached", report_world, _report_request(), _expect_images,
             install=_images_everywhere,
             script=Script(cite=r"k1|k4\d{3}|k5\d{3}", reflects=(ENUMERATE_CLAIMS,))),
    Scenario("answer_client_unconfigured", library_world, plain(), _expect_answer_unconfigured, stream=True,
             install=_answer_unconfigured),
    Scenario("hybrid_answer_client_unconfigured", knowhow_world,
             confirmed(LIST_ALL, topics=(), result_scope="hybrid", completeness=True),
             _expect_hybrid_answer_unconfigured, install=_answer_unconfigured),
    Scenario("synthesis_failed_without_hits", documents_world, plain(), _expect_synthesis_failed_without_hits,
             script=Script(failures={"ask_answer": RuntimeError("answer model down")})),
    Scenario("related_knowledge_capped", library_world, plain(), _expect_related_capped,
             env={"ASK_RELATED_KNOWLEDGE_LIMIT": "2"},
             install=_doctored(_duplicate_first_hit)),
    Scenario("federated_hit", peer_world, plain(), _expect_federated,
             install=_doctored(_first_hit_from_peer)),
    Scenario("cancelled_in_draft_first_check", library_world, plain(), _expect_cancel_first_check,
             install=_cancel_after("self._reasoning_report_turn")),
    Scenario("cancelled_in_draft_before_model", library_world, plain(), _expect_cancel_before_model,
             install=_cancel_after("evidence.citations_from")),
    Scenario("cancelled_between_sections", library_world, _exhaustive(), _expect_cancel_between_sections,
             stream=True, install=_cancel_after("self._external_exact_keys"),
             script=Script(cite=ANY_KEY, reflects=(_outline(*SECTIONS),))),
    Scenario("draft_called_directly", library_world, plain(), _expect_draft_called_directly,
             invoke=_direct_draft),
]


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
    Scenario("activation_fails_soft_streamed", library_world, _narrowed,
             _expect_activation_fails_streamed, stream=True, install=_activation_fails),
    Scenario("activation_fails_on_empty_retrieval", library_world, _narrowed,
             _expect_activation_fails_on_empty_retrieval, stream=True,
             install=_activation_fails_on_empty_retrieval),
    Scenario("cancelled_during_activation", library_world, _narrowed,
             _expect_cancel_during_activation, install=_cancel_during_activation),
    Scenario("activation_control_error", library_world, _narrowed,
             _expect_activation_refused, install=_activation_refused),
    Scenario("graph_service_control_error", library_world, _narrowed_include,
             _expect_graph_service_refused, install=_refuse_in_graph_service),
    Scenario("report_graph_service_control_error", library_world, _narrowed,
             _expect_report_graph_service_refused, install=_refuse_in_graph_service,
             invoke=_graph_service_refused_in_report),
    Scenario("unconfigured_completeness", library_world,
             confirmed(LIST_ALL, topics=(), result_scope="complete", completeness=True),
             _expect_unconfigured_completeness, install=_unconfigured),
    Scenario("invalid_stage_inputs", library_world, plain(), _expect_invalid_inputs,
             invoke=_invalid_inputs),
    *DRAFT_SCENARIOS,
]
