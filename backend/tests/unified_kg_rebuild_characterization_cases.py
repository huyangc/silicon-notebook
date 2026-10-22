"""Behaviour rig for ``KnowledgeLifecycleService.rebuild_unified_kg``.

**两端吃同一份世界、同一套观测、同一组场景**:``tests/test_unified_kg_rebuild_characterization.py``
(SQLite)与 ``tests/postgres/test_unified_kg_rebuild_characterization_pg.py``(PostgreSQL)
各自只写一个 ``Backend`` 适配器(有哪些表、占位符)。不是测试模块(没有 ``test_`` 前缀),
pytest 不会收集它。

``observe`` 录下 rebuild 在本线程对外的调用,按发生顺序:对 store(unified_kg / governance /
knowledge)、scale_artifacts、event_log、模型客户端的每一次调用(参数与返回值)、对本服务自身
协作方法与 ``kg_merge`` / ``concept_merge_review`` 模块函数的调用,外加模型收到的提示词、本线程
的日志、进度回调、rebuild 前后有变化的表名与返回值/异常。``Scenario.expect`` 只断言其中用户与
下游可见的事实——返回的簇数或抛出的异常、发布代是否翻转、在飞认领在每个出口都已释放(取号之后
任何一步失败也一样)、两张 scratch 表不留本轮的行、``kg_mutation_seq`` 不被 rebuild 推进(期间
到达的变更让 ``dirty`` 保持 1)、发布出来的合并分组与待审候选、跳过路径零重算、派生尾部(规范
关系、共提桥接、viz、自动索引、社区)按序以 ``force=True`` 调度且各自 fail-open、checkpoint 的
保留/清空。

这套世界与场景最初是 ``rebuild_unified_kg`` 零行为变化拆分的表征:拆分前在两端采集的逐字节
快照(另含本线程的每条 SQL、id 铸造、取时钟与整库/存储目录差分)拆分后不变,随后按 AGENTS.md
「不做只为重构服务的金标快照」退役,只留本行为层。

时钟:造世界固定为 ``SEED_NOW``、被测 rebuild 固定为 ``REBUILD_NOW``(远未来,让本轮期间的
并发追加落进催收窗口)。日志与进度里的耗时记成 ``<dur>s``。模型调用另记一条全线程通道
(工作线程固定为 1)。
"""
from __future__ import annotations

import collections
import dataclasses
import hashlib
import json
import logging
import re
import threading
from typing import Any, Callable, Optional

from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_all_embedding_clients

SEED_NOW = "2026-09-30T00:00:00+00:00"
REBUILD_NOW = "2999-01-01T00:00:00+00:00"
DEBT_TS = "2026-09-30 00:00:00"
EMBED_DIM = 16
MISSING_NB = "nb-missing"

# ------------------------------------------------------------------ models


def _unit(*weights: tuple) -> list[float]:
    vec = [0.0] * EMBED_DIM
    for index, weight in weights:
        vec[index] += weight
    return vec


#: Concept names (lower-cased substring keys, longest first) → designed vectors:
#: MOSFET and its spelled-out name are near duplicates (an auto candidate the LLM
#: merges), FinFET/GAAFET sit in the pending band, NMOS/PMOS are near duplicates a
#: contrast group blocks, "bandgap reference" has no vector at all (name-only seed),
#: Nanosheet FET sits in GAAFET's pending band (a deferred decision holds it back).
VECTORS = (
    ("metal oxide semiconductor fet", _unit((0, 1.0), (1, 0.1))),
    ("tunnel fet", _unit((9, 1.0))),
    ("nmos switch", _unit((7, 1.0))),
    ("pmos switch", _unit((7, 1.0), (8, 0.05))),
    ("mosfet", _unit((0, 1.0))),
    ("finfet", _unit((0, 1.0), (2, 0.6))),
    ("nanosheet fet", _unit((2, 1.0), (0, 0.6), (3, 0.5))),
    ("gaafet", _unit((2, 1.0), (0, 0.6))),
    ("cascode", _unit((5, 1.0))),
)


class DesignedEmbedder:
    configured = True
    dim = EMBED_DIM

    def _vec(self, text: str) -> list[float]:
        lowered = (text or "").lower()
        for key, vec in VECTORS:
            if key in lowered:
                return list(vec)
        digest = hashlib.sha256(lowered.encode("utf-8")).digest()
        return _unit((10 + digest[0] % 6, 1.0))

    def embed_texts(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


class FakeChat:
    """One workload's chat client: scripted JSON replies, every prompt recorded."""

    configured = True
    model = "fake-rebuild-model"

    def __init__(self, workload: str, llm: "FakeModels") -> None:
        self.workload, self._llm = workload, llm

    def chat_json(self, messages, schema_hint=None, **kwargs):
        prompt = messages[-1]["content"]
        reply = self._llm.reply(self.workload, prompt)
        self._llm.note({"workload": self.workload, "prompt": prompt, "reply": reply})
        return reply


class FakeModels:
    """Stand-in for ``lifecycle.model_clients``: ``configured`` per workload, one worker."""

    def __init__(self, configured: frozenset, recorder: "Recorder") -> None:
        self.configured_workloads = configured
        self.recorder = recorder
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def note(self, entry: dict) -> None:
        if self.recorder.active:
            with self._lock:
                self.calls.append(entry)

    def configured(self, workload: str) -> bool:
        return workload in self.configured_workloads

    def chat(self, workload: str) -> FakeChat:
        return FakeChat(workload, self)

    def parallelism(self, workload: str) -> int:
        return 1

    @staticmethod
    def reply(workload: str, prompt: str) -> str:
        if workload == "kg_merge_review":
            decisions = []
            for block in re.findall(r"- id=(\S+) score=\S+\n  A: (.*)\n  B: (.*)", prompt):
                cid, a, b = block
                # The prompt names the pair by canonical id, so its spaces are
                # those of the seed space (concept seed space 2 squashes them:
                # "K-metaloxidesemiconductorfet"); the script reads both forms.
                pair = {a.lower().replace(" ", ""), b.lower().replace(" ", "")}
                # "mosfet" folds to the alias seed "mos transistor" ("mostransistor").
                same = (any("metaloxide" in p for p in pair)
                        and any("mos" in p and "metal" not in p for p in pair))
                decisions.append({
                    "candidate_id": cid,
                    "decision": "merge" if same else "keep_separate",
                    "confidence": 0.97 if same else 0.95,
                    "rationale": "scripted",
                })
            return json.dumps({"decisions": decisions})
        if workload == "kg_concept_description":
            if "cascode" in prompt.lower():
                return "not json"
            name = re.search(r"[A-Za-z][A-Za-z ]+", prompt)
            return json.dumps({"description": f"描述 {len(prompt)}"
                               + (f" {name.group(0)[:12]}" if name else "")})
        raise AssertionError(f"unexpected workload {workload}")


LLM_ALL = frozenset({"kg_merge_review", "kg_concept_description"})

# ------------------------------------------------------------- normalisation

_DURATION = re.compile(r"\d+\.\ds\b")


def plain(value: Any) -> Any:
    """What an assertion reads of a recorded value: containers as lists/dicts,
    stage durations as ``<dur>s``, anything else opaque reduced to its type name
    (no live cursor or vector is kept on the timeline)."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _DURATION.sub("<dur>s", value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((plain(v) for v in value), key=repr)
    return f"<{type(value).__name__}>"


# ------------------------------------------------------------------ recorder


class Recorder:
    """One scenario's ordered timeline; ``active`` gates every channel."""

    def __init__(self) -> None:
        self.active = False
        self.thread = threading.get_ident()
        self.timeline: list[dict] = []

    def on_thread(self) -> bool:
        return self.active and threading.get_ident() == self.thread

    def note(self, entry: dict) -> dict:
        if self.on_thread():
            self.timeline.append(entry)
        return entry


class Hooks:
    """Per-scenario interception keyed ``"<label>.<method>"``: ``before``/``after``
    side effects and ``replace`` (receives the real callable)."""

    def __init__(self) -> None:
        self.before: dict[str, Callable] = {}
        self.after: dict[str, Callable] = {}
        self.replace: dict[str, Callable] = {}


def _recorded(label: str, name: str, attr: Callable, recorder: Recorder,
              hooks: Hooks) -> Callable:
    key = f"{label}.{name}"

    def call(*args: Any, **kwargs: Any) -> Any:
        if not recorder.active:
            return attr(*args, **kwargs)
        entry = recorder.note({"call": key, "args": plain(args), "kwargs": plain(kwargs)})
        if key in hooks.before:
            hooks.before[key](*args, **kwargs)
        try:
            if key in hooks.replace:
                result = hooks.replace[key](attr, *args, **kwargs)
            else:
                result = attr(*args, **kwargs)
        except BaseException as exc:
            entry["raised"] = f"{type(exc).__name__}: {exc}"
            raise
        if key in hooks.after:
            hooks.after[key](result, *args, **kwargs)
        entry["ret"] = plain(result)
        return result

    return call


class _Spy:
    """Transparent recording proxy for one collaborator object."""

    def __init__(self, label: str, target: Any, recorder: Recorder, hooks: Hooks) -> None:
        self._label, self._target, self._recorder, self._hooks = label, target, recorder, hooks

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._target, name)
        if name.startswith("__") or not callable(attr):
            return attr
        return _recorded(self._label, name, attr, self._recorder, self._hooks)


# ----------------------------------------------------------------- the world


def _evidence(source: str, span: str) -> list[dict]:
    return [{"source_id": source, "source_title": source, "element_id": f"el-{source}",
             "element_type": "paragraph", "location_label": "p1", "quoted_span": span,
             "confidence": 1.0}]


def _obj(local_id: str, object_type: str, name: str, source: str, span: str,
         **payload: Any) -> dict:
    return {"local_id": local_id, "object_type": object_type,
            "payload": {"name": name, "section_path": "1", **payload},
            "evidence": _evidence(source, span)}


def _rel(a: str, b: str, edge: str, source: str) -> dict:
    return {"source_local_id": a, "target_local_id": b, "edge_type": edge,
            "evidence": _evidence(source, f"{a} {edge} {b}")}


STEPS = [{"name": "bias the gate"}, {"name": "sweep the drain"}]

S1_OBJECTS = [
    _obj("a-mos", "concept", "MOSFET", "s1", "MOSFET switches current"),
    _obj("a-fin", "concept", "FinFET", "s1", "FinFET wraps the channel"),
    _obj("a-cas", "concept", "cascode", "s1", "cascode raises output impedance"),
    _obj("a-nmos", "concept", "NMOS switch", "s1", "NMOS switch pulls down"),
    _obj("a-bg", "concept", "bandgap reference", "s1", "bandgap reference is stable"),
    _obj("a-clm", "claim", "Vth drops with temperature", "s1", "Vth drops"),
    _obj("a-frm", "formula", "I_D = k (V_GS - V_th)^2", "s1", "square law"),
    _obj("a-prc", "procedure", "Measure Vth", "s1", "measure it", steps=STEPS),
]
S1_RELATIONS = [_rel("a-mos", "a-fin", "related_to", "s1"),
                _rel("a-cas", "a-mos", "uses", "s1"),
                _rel("a-clm", "a-mos", "mentions", "s1")]
S2_OBJECTS = [
    _obj("b-mos", "concept", "mosfet", "s2", "a mosfet has a gate"),
    _obj("b-moslong", "concept", "Metal Oxide Semiconductor FET", "s2", "metal oxide gate"),
    _obj("b-gaa", "concept", "GAAFET", "s2", "GAAFET surrounds the channel"),
    _obj("b-pmos", "concept", "PMOS switch", "s2", "PMOS switch pulls up"),
    _obj("b-nano", "concept", "Nanosheet FET", "s2", "nanosheets stack the channel"),
    _obj("b-cas", "concept", "cascode", "s2", "a cascode stage"),
    _obj("b-old", "concept", "Deprecated thing", "s2", "gone"),
    _obj("b-clm", "claim", "Vth drops with temperature.", "s2", "it drops"),
    _obj("b-frm", "formula", "I_D = k(V_GS - V_th)^2", "s2", "square law again"),
    _obj("b-prc", "procedure", "measure vth", "s2", "measure again", steps=STEPS),
]
S2_RELATIONS = [_rel("b-gaa", "b-mos", "related_to", "s2"),
                _rel("b-clm", "b-cas", "mentions", "s2")]


@dataclasses.dataclass
class Rig:
    repo: Any
    backend: "Backend"
    recorder: Recorder
    hooks: Hooks
    models: FakeModels
    notebook_id: str = ""

    @property
    def lifecycle(self) -> Any:
        return self.repo._runtime.knowledge_lifecycle

    def sql(self, statement: str, params: tuple = ()) -> list:
        statement = statement.replace("?", self.backend.placeholder)
        with self.repo._runtime.database.write() as db:
            cur = db.execute(statement, params)
            try:
                return [dict(r) for r in cur.fetchall()]
            except Exception:  # noqa: BLE001 — statements without rows
                return []

    def query(self, statement: str, params: tuple = ()) -> list[dict]:
        statement = statement.replace("?", self.backend.placeholder)
        with self.repo._runtime.database.connect() as db:
            return [dict(r) for r in db.execute(statement, params).fetchall()]

    def add_source(self, source_id: str) -> None:
        self.sql("INSERT INTO sources (id, notebook_id, title, source_type, status, parse_status, "
                 "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                 (source_id, self.notebook_id, source_id, "document", "ready", "ready",
                  SEED_NOW, SEED_NOW))


def kg_world(rig: Rig) -> None:
    """Two sources whose concepts overlap by seed and by vector, three non-concept
    types with cross-source duplicates, a deprecated object, a name-only concept,
    a confirmed, a rejected and a deferred human decision."""
    nb = rig.repo.create_notebook(NotebookCreate(name="rebuild world")).id
    rig.notebook_id = nb
    lifecycle = rig.lifecycle
    for source_id in ("s1", "s2", "s3"):
        rig.add_source(source_id)
    lifecycle.store_kg(nb, "s1", [dict(o) for o in S1_OBJECTS], list(S1_RELATIONS))
    lifecycle.store_kg(nb, "s2", [dict(o) for o in S2_OBJECTS], list(S2_RELATIONS))
    rig.sql("UPDATE knowledge_objects SET status='deprecated' "
            "WHERE notebook_id=? AND payload LIKE ?"
            if rig.backend.name == "sqlite" else
            "UPDATE knowledge_objects SET status='deprecated' "
            "WHERE notebook_id=? AND payload->>'name' = ?",
            (nb, '%Deprecated thing%' if rig.backend.name == "sqlite" else "Deprecated thing"))
    bandgap = [o["id"] for o in rig.query(
        "SELECT id, payload FROM knowledge_objects WHERE notebook_id=? ORDER BY id", (nb,))
        if "bandgap" in json.dumps(_json(o["payload"]))]
    for oid in bandgap:
        rig.sql("DELETE FROM knowledge_embeddings WHERE object_id=?", (oid,))
    for cid, a, b, status in (("mc-seed-confirmed", "cascode", "bandgap reference", "confirmed"),
                              ("mc-seed-rejected", "finfet", "gaafet", "rejected"),
                              # Not a candidate itself; it tells a correct live refresh
                              # from one that reads its decisions crossed.
                              ("mc-seed-rejected-far", "gaafet", "mos transistor", "rejected"),
                              ("mc-seed-deferred", "gaafet", "nanosheet fet", "deferred")):
        rig.sql("INSERT INTO concept_merge_candidates (id,notebook_id,canonical_a,canonical_b,"
                "seed_a,seed_b,score,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (cid, nb, f"K-{a}", f"K-{b}", a, b, 0.9, status, SEED_NOW, SEED_NOW))


def kg_world_rebuilt(times: int = 1) -> Callable[[Rig], None]:
    def build(rig: Rig) -> None:
        kg_world(rig)
        for index in range(times):
            rig.lifecycle.rebuild_unified_kg(rig.notebook_id, force=index > 0)
    return build


def empty_world(rig: Rig) -> None:
    rig.notebook_id = rig.repo.create_notebook(NotebookCreate(name="empty")).id


def empty_world_rebuilt(rig: Rig) -> None:
    empty_world(rig)
    rig.lifecycle.rebuild_unified_kg(rig.notebook_id)


def missing_world(rig: Rig) -> None:
    kg_world(rig)
    rig.notebook_id = MISSING_NB


def _json(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in ("[", "{"):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


# ------------------------------------------------------------- observation


@dataclasses.dataclass(frozen=True)
class Scenario:
    name: str
    world: Callable[[Rig], None]
    expect: Callable[["Observation"], None]
    force: bool = False
    fresh: bool = False
    progress: Optional[str] = "record"        # None | "record" | "raise"
    models: frozenset = LLM_ALL
    install: Optional[Callable[[Rig], None]] = None
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
    outcome: dict
    timeline: list
    llm: list
    progress: list
    #: Tables whose rows differ after the rebuild (whole database, both directions).
    changed_tables: list
    logs: list
    before: dict
    after: dict

    def calls(self, key: str) -> list[dict]:
        return [e for e in self.timeline if e.get("call") == key]

    def call_names(self, prefix: str) -> list[str]:
        return [e["call"] for e in self.timeline if e.get("call", "").startswith(prefix)]

    def events(self) -> list[dict]:
        return [e["args"][0] for e in self.calls("event_log.emit")]

    def event_kinds(self) -> list[str]:
        return [ev["kind"] for ev in self.events()]


#: The rebuild's own tables a behaviour assertion reads after the run.
_FACT_QUERIES = {
    "state": "SELECT * FROM unified_kg_state WHERE notebook_id=?",
    "published": (
        "SELECT c.object_type, c.canonical_id, c.canonical_name, c.canonical_description, "
        "c.canonical_desc_sig, "
        "o.payload FROM concept_clusters c JOIN knowledge_objects o ON o.id = c.member_object_id "
        "WHERE c.notebook_id=? AND c.generation = COALESCE((SELECT cluster_generation "
        "FROM unified_kg_state WHERE notebook_id=c.notebook_id), 0) "
        "ORDER BY c.object_type, c.canonical_id, o.id"),
    "generations": ("SELECT generation, COUNT(*) AS n FROM concept_clusters "
                    "WHERE notebook_id=? GROUP BY generation ORDER BY generation"),
    "pending": ("SELECT seed_a, seed_b FROM concept_merge_candidates "
                "WHERE notebook_id=? AND status='pending' ORDER BY seed_a, seed_b"),
    "decided": ("SELECT seed_a, seed_b, status FROM concept_merge_candidates "
                "WHERE notebook_id=? AND status <> 'pending' ORDER BY seed_a, seed_b"),
    "scratch": "SELECT COUNT(*) AS n FROM kg_cluster_scratch WHERE notebook_id=?",
    "canonical_scratch": "SELECT COUNT(*) AS n FROM kg_canonical_scratch WHERE notebook_id=?",
    "checkpoints": ("SELECT stage, COUNT(*) AS n FROM kg_rebuild_checkpoint "
                    "WHERE notebook_id=? GROUP BY stage ORDER BY stage"),
    "checkpoint_versions": ("SELECT DISTINCT input_version FROM kg_rebuild_checkpoint "
                            "WHERE notebook_id=? ORDER BY input_version"),
}


def _facts(rig: Rig) -> dict:
    out = {}
    for key, statement in _FACT_QUERIES.items():
        rows = rig.query(statement, (rig.notebook_id,))
        out[key] = [{k: _json(v) for k, v in row.items()} for row in rows]
    return out


def _table_rows(repo: Any, backend: Backend) -> dict[str, collections.Counter]:
    out: dict[str, collections.Counter] = {}
    with repo._runtime.database.connect() as db:
        for table in backend.tables(repo):
            rows = db.execute(f"SELECT * FROM {table}").fetchall()
            out[table] = collections.Counter(
                json.dumps(dict(row), sort_keys=True, default=repr) for row in rows)
    return out


def _changed_tables(before: dict, after: dict) -> list[str]:
    return sorted(t for t in set(before) | set(after)
                  if before.get(t, collections.Counter()) != after.get(t, collections.Counter()))


class _LogCapture(logging.Handler):
    def __init__(self, recorder: Recorder) -> None:
        super().__init__(logging.DEBUG)
        self.recorder = recorder
        self.records: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        # Only the rebuild's own thread: a background job elsewhere in the
        # process must not land in (or break) an exact-equality assertion.
        if not self.recorder.active or record.thread != self.recorder.thread:
            return
        entry = {"logger": record.name, "level": record.levelname,
                 "message": plain(record.getMessage())}
        if record.exc_info and record.exc_info[0] is not None:
            entry["exc"] = record.exc_info[0].__name__
        self.records.append(entry)


def _seam_modules(repo: Any) -> list[Any]:
    import sys

    from app.services import repository_facade

    modules = [repository_facade]
    concrete = sys.modules.get(type(repo).__module__)
    if concrete is not None and concrete is not repository_facade:
        modules.append(concrete)
    return modules


#: Collaborator objects held by the lifecycle service, recorded at the boundary.
_COLLABORATORS = ("unified_kg", "governance_store", "knowledge", "scale_artifacts",
                  "event_log")

#: Methods of the service itself the rebuild calls (instance-level wrappers).
_SELF_METHODS = (
    "get_notebook", "_stream_concept_seed_space", "_squash_merge_candidates",
    "_refresh_pending_merges", "_cluster_input_facts", "_cluster_input_version",
    "_settle_skip_path",
    "_settle_generation_catchup", "_reap_stale_cluster_generations", "_notebook_deleting",
    "_refresh_generation_lease", "_stream_seed_reps", "_concept_description_stage",
    "_write_cluster_map_streamed", "_invalidate_unified_cache", "_refresh_viz_after_rebuild",
    "rebuild_canonical_relations", "rebuild_mention_bridge", "rebuild_communities",
    "append_clusters", "_bulk_write",
)

#: Module functions the rebuild imports at call time.
_MODULE_FUNCTIONS = (
    ("app.services.kg_merge", ("cluster_seeds", "filter_pending_seeds_by_decisions",
                               "cluster_code_version")),
    ("app.services.concept_merge_review", ("review_merge_candidates",)),
)


def _install_recording(rig: Rig, monkeypatch: Any) -> None:
    import importlib

    recorder, hooks, lifecycle = rig.recorder, rig.hooks, rig.lifecycle
    for name in _COLLABORATORS:
        monkeypatch.setattr(lifecycle, name, _Spy(name, getattr(lifecycle, name),
                                                  recorder, hooks))
    for name in _SELF_METHODS:
        monkeypatch.setattr(lifecycle, name,
                            _recorded("self", name, getattr(lifecycle, name), recorder, hooks))
    for module_name, names in _MODULE_FUNCTIONS:
        module = importlib.import_module(module_name)
        for name in names:
            monkeypatch.setattr(module, name, _recorded(
                "fn", name, getattr(module, name), recorder, hooks))
    monkeypatch.setattr(lifecycle, "model_clients", _Spy("models", rig.models, recorder, hooks))


def observe(repo: Any, backend: Backend, scenario: Scenario, monkeypatch: Any) -> Observation:
    """Build the scenario's world, run one rebuild with every collaborator recorded,
    and return the whole observation."""
    recorder = Recorder()
    hooks = Hooks()
    models = FakeModels(scenario.models, recorder)
    rig = Rig(repo=repo, backend=backend, recorder=recorder, hooks=hooks, models=models)
    bind_all_embedding_clients(repo, DesignedEmbedder())

    clock = {"now": SEED_NOW}

    def now() -> str:
        return clock["now"]

    for module in _seam_modules(repo):
        monkeypatch.setattr(module, "_now", now, raising=False)
    _install_recording(rig, monkeypatch)

    scenario.world(rig)
    if scenario.install is not None:
        scenario.install(rig)
    backend.invalidate_caches()
    lifecycle = rig.lifecycle
    lifecycle.unified_cache.clear()
    before_facts = _facts(rig) if rig.notebook_id != MISSING_NB else {}
    db_before = _table_rows(repo, backend)
    building_before = set(lifecycle.kg_building)

    progress_calls: list = []

    def progress(message: str, done: int, total: int) -> None:
        progress_calls.append([plain(message), done, total])
        if scenario.progress == "raise":
            raise RuntimeError("progress sink failed")

    loggers = [logging.getLogger("silicon_notebook"), logging.getLogger("app")]
    capture = _LogCapture(recorder)
    levels = [lg.level for lg in loggers]
    for lg in loggers:
        lg.addHandler(capture)
        lg.setLevel(logging.DEBUG)
    clock["now"] = REBUILD_NOW
    try:
        recorder.active = True
        try:
            count = lifecycle.rebuild_unified_kg(
                rig.notebook_id,
                progress=None if scenario.progress is None else progress,
                force=scenario.force, fresh=scenario.fresh,
            )
            outcome = {"returned": count}
        except Exception as exc:  # noqa: BLE001 — the raise IS the observation
            outcome = {"raised": type(exc).__name__, "message": str(exc),
                       "attrs": plain({k: v for k, v in vars(exc).items()
                                       if not k.startswith("_")})}
        finally:
            recorder.active = False
    finally:
        for lg, level in zip(loggers, levels):
            lg.removeHandler(capture)
            lg.setLevel(level)
    db_after = _table_rows(repo, backend)
    after_facts = _facts(rig) if rig.notebook_id != MISSING_NB else {}
    assert set(lifecycle.kg_building) == building_before, "rebuild never touches kg_building"
    return Observation(
        notebook_id=rig.notebook_id,
        outcome=outcome,
        timeline=recorder.timeline,
        llm=models.calls,
        progress=progress_calls,
        changed_tables=_changed_tables(db_before, db_after),
        logs=capture.records,
        before=before_facts,
        after=after_facts,
    )


# ------------------------------------------------------- the behaviour layer

#: The derived tail after a real rebuild, in order, every step forced.
DERIVED_TAIL = ["self.rebuild_canonical_relations", "self.rebuild_mention_bridge",
                "self._refresh_viz_after_rebuild", "scale_artifacts.maybe_auto_index",
                "self.rebuild_communities"]


def _tail(obs: Observation) -> list[str]:
    keys = set(DERIVED_TAIL)
    return [e["call"] for e in obs.timeline if e.get("call") in keys]


def _groups(obs: Observation, object_type: str = "concept") -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for row in obs.after["published"]:
        if row["object_type"] == object_type:
            out.setdefault(row["canonical_name"], []).append(row["payload"]["name"])
    return {k: sorted(v) for k, v in sorted(out.items())}


def _state(obs: Observation, which: str = "after") -> dict:
    rows = getattr(obs, which)["state"]
    return rows[0] if rows else {}


def _pending(obs: Observation) -> set:
    return {frozenset((r["seed_a"], r["seed_b"])) for r in obs.after["pending"]}


def _is_debt(value: Any) -> bool:
    """The seeded catch-up marker (SQLite text, PostgreSQL timestamptz)."""
    return value is not None and str(value)[:10] == DEBT_TS[:10]


def _assert_claim_released(obs: Observation) -> None:
    assert _state(obs)["derived_building_generation"] == 0, _state(obs)
    assert obs.after["scratch"] == [{"n": 0}] and obs.after["canonical_scratch"] == [{"n": 0}]


def _assert_published(obs: Observation, *, merged_by_llm: bool, writes_mid_run: int = 0) -> None:
    """A real rebuild: published generation flipped to the claimed one, inputs
    version stored, mutation seq untouched by the rebuild itself (only by KG writes
    a scenario lands mid-run), derived tail forced in order."""
    before, after = _state(obs, "before"), _state(obs)
    claim = obs.calls("unified_kg.claim_derived_generation")[0]["ret"]
    assert after["cluster_generation"] == claim["generation"]
    assert after["kg_mutation_seq"] == (before or {}).get(
        "kg_mutation_seq", after["kg_mutation_seq"] - writes_mid_run) + writes_mid_run
    versions = obs.calls("self._cluster_input_version")
    (entry,) = [e for e in versions if set(e["kwargs"]) == {"facts"}]
    (stored,) = [e for e in versions if "dec_growth" in e["kwargs"]]
    # The END-write stores the version of the inputs read at entry (the same
    # facts), plus only the rows the v1 decision recompute added (S1 step 5);
    # without such rows it IS the entry version.
    assert stored["kwargs"]["facts"] == entry["kwargs"]["facts"]
    assert after["cluster_input_version"] == stored["ret"]
    if stored["kwargs"]["dec_growth"] == 0:
        assert stored["ret"] == entry["ret"]
    assert obs.outcome["returned"] == after["cluster_count"]
    _assert_claim_released(obs)
    assert _tail(obs) == DERIVED_TAIL
    for key in ("self.rebuild_canonical_relations", "self.rebuild_mention_bridge",
                "self.rebuild_communities"):
        assert obs.calls(key)[-1]["kwargs"]["force"] is True, key
    groups = _groups(obs)
    mos = ["MOSFET", "mosfet"] + (["Metal Oxide Semiconductor FET"] if merged_by_llm else [])
    assert sorted(mos) in groups.values(), groups
    assert ["bandgap reference", "cascode", "cascode"] in groups.values(), groups
    assert ["NMOS switch"] in groups.values() and ["PMOS switch"] in groups.values(), groups
    assert ["Nanosheet FET"] in groups.values(), groups
    # The deferred pair stays out of the pending queue, like a rejected one --
    # in either seed space (the world's deferral is a v1 row the first rebuild
    # recomputes into seed space 2).
    assert not {frozenset(("gaafet", "nanosheet fet")),
                frozenset(("gaafet", "nanosheetfet"))} & _pending(obs)
    assert not any("Deprecated thing" in v for v in groups.values()), groups
    for object_type in ("claim", "formula", "procedure"):
        assert len(_groups(obs, object_type)) == 1, (object_type, _groups(obs, object_type))
    # Canonical ids keep each type's prefix (types never share an id space).
    prefixes = {"concept": "K-", "claim": "KL-", "formula": "KF-", "procedure": "KP-"}
    for row in obs.after["published"]:
        assert row["canonical_id"].startswith(prefixes[row["object_type"]]), row
    # Every stage boundary renews the claim (the heartbeat that keeps TTL
    # pre-emption for dead rebuilds only), and the keyed caches are dropped
    # right after the flip.
    stage_lines = [log for log in obs.logs if log["message"].startswith(
        f"kg-rebuild[{obs.notebook_id}] ")]
    assert len(obs.calls("self._refresh_generation_lease")) >= len(stage_lines) > 0
    calls = [e.get("call") for e in obs.timeline]
    flip_at = calls.index("unified_kg.flip_cluster_generation")
    assert "self._invalidate_unified_cache" in calls[flip_at:], "cache kept after flip"
    # The skip gate reads its "inputs unchanged" verdict only from this state.
    assert after["cluster_code_version"]


def _stages(obs: Observation) -> list[str]:
    return [p[0] for p in obs.progress if p[1:] == [0, 0]]


def expect_first_rebuild(obs: Observation) -> None:
    _assert_published(obs, merged_by_llm=True)
    assert obs.outcome["returned"] == 7
    assert _state(obs)["dirty"] == 0
    # Seed space 2 keys (the world's v1 decisions are recomputed in this first
    # rebuild): "mos transistor" is "mostransistor" there.
    assert _pending(obs) == {frozenset(("finfet", "mostransistor"))}, _pending(obs)
    # Generation 1: nothing to pre-reap; merge review and descriptions both ran.
    assert not obs.calls("self._reap_stale_cluster_generations")
    assert [c["workload"] for c in obs.llm][:1] == ["kg_merge_review"]
    descriptions = {r["canonical_name"]: r["canonical_description"]
                    for r in obs.after["published"] if r["object_type"] == "concept"}
    assert descriptions["MOSFET"] and not descriptions["cascode"], descriptions
    # The reuse key is stored next to the text (the next rebuild reuses by it).
    sigs = {r["canonical_name"]: r["canonical_desc_sig"]
            for r in obs.after["published"] if r["object_type"] == "concept"}
    assert sigs["MOSFET"] and not sigs["cascode"], sigs
    # Both LLM stages checkpoint under the one embedding-blind version.
    assert len(obs.after["checkpoint_versions"]) == 1, obs.after["checkpoint_versions"]
    assert [p for p in obs.progress if p[0] == "concept_desc"] == [
        ["concept_desc", 1, 2], ["concept_desc", 2, 2]]
    assert [s.split(" (")[0] for s in _stages(obs)] == [
        "concept: streamed 11 objs → 9 seeds, 8 vecs",
        "concept: merge-review 1 candidates → 1 merged",
        "concept: clustered 9 seeds → 7 canonicals, 0 auto-cand",
        "concept: descriptions 1",
        "concept: wrote clusters",
        "claim: streamed+clustered+wrote",
        "formula: streamed+clustered+wrote",
        "procedure: streamed+clustered+wrote",
        "pending refresh",
        "flipped cluster generation 0 → 1",
        "DONE 7 canonicals, total <dur>s",
    ], _stages(obs)
    assert {row["stage"] for row in obs.after["checkpoints"]} == {"concept_desc", "merge_review"}


def expect_skip(obs: Observation) -> None:
    """Inputs unchanged: nothing is reclustered and nothing is written; the three
    derived layers still get their own (gated) incremental pass."""
    assert obs.outcome["returned"] == _state(obs, "before")["cluster_count"]
    assert not obs.calls("unified_kg.claim_derived_generation")
    assert not obs.calls("self._stream_seed_reps")
    assert [e["call"] for e in obs.timeline if e.get("call") in {
        "self.rebuild_canonical_relations", "self.rebuild_mention_bridge",
        "self.rebuild_communities"}] == [
        "self.rebuild_canonical_relations", "self.rebuild_mention_bridge",
        "self.rebuild_communities"]
    for key in ("self.rebuild_canonical_relations", "self.rebuild_mention_bridge"):
        assert obs.calls(key)[0]["kwargs"] == {}, key
    assert obs.calls("self.rebuild_communities")[0]["kwargs"] == {"level": 0}
    assert obs.after["published"] == obs.before["published"]
    assert obs.after["state"] == obs.before["state"]
    assert obs.progress == [["跳过:自上次 rebuild 起输入未变化(7 clusters)", 0, 0]]
    assert not obs.llm


def expect_skip_settles_debt(obs: Observation) -> None:
    assert obs.outcome["returned"] == _state(obs, "before")["cluster_count"]
    assert not obs.calls("self._stream_seed_reps")
    before, after = _state(obs, "before"), _state(obs)
    assert _is_debt(before["derived_catchup_from"]) and after["derived_catchup_from"] is None
    assert before["cluster_code_version"] == "" and after["cluster_code_version"]
    assert after["derived_building_generation"] == 0
    assert after["derived_generation_counter"] == before["derived_generation_counter"] + 1
    assert after["cluster_generation"] == before["cluster_generation"]


def expect_skip_derived_fail(obs: Observation) -> None:
    assert obs.outcome["returned"] == 7
    assert obs.event_kinds() == ["canonical_relations_rebuild_failed",
                                 "mention_bridge_rebuild_failed",
                                 "communities_rebuild_failed"], obs.event_kinds()
    assert {ev["error"] for ev in obs.events()} == {"injected derived failure"}


def expect_forced_second(obs: Observation) -> None:
    """Third generation over two retired ones: pre-reap keeps (published, in-flight),
    the checkpointed adjudication is reused (no new merge-review call), a decision
    and a KG write that land mid-rebuild are honoured, the concurrent append is
    caught up into the new generation and counted."""
    _assert_published(obs, merged_by_llm=True, writes_mid_run=1)
    before, after = _state(obs, "before"), _state(obs)
    # Relative to this round's claim (the generation counter is shared with the
    # community layer, so absolute numbers say nothing): the new generation is
    # published, pre-reaping kept exactly (previously published, in-flight), and
    # only those two generations remain.
    claimed = obs.calls("unified_kg.claim_derived_generation")[0]["ret"]["generation"]
    published_before = before["cluster_generation"]
    assert published_before > 0 and claimed > published_before
    assert after["cluster_generation"] == claimed
    keep = obs.calls("self._reap_stale_cluster_generations")[0]["kwargs"]["keep"]
    assert set(keep) == {published_before, claimed}, keep
    assert [r["generation"] for r in obs.after["generations"]] == sorted(
        {published_before, claimed})
    assert "kg_generation_reaped" in obs.event_kinds()
    # The checkpointed adjudication is reused; only the description whose reply was
    # unusable last time (cascode) is asked again, MOSFET's is reused by signature.
    assert [c["workload"] for c in obs.llm] == ["kg_concept_description"], obs.llm
    assert "cascode" in obs.llm[0]["prompt"]
    # The live rejection filtered the pending pair out in the refresh transaction.
    assert _pending(obs) == set(), _pending(obs)
    # A write mid-rebuild: the seq moved past the claim, so dirty stays 1.
    assert after["kg_mutation_seq"] == before["kg_mutation_seq"] + 1
    assert after["dirty"] == 1
    assert ["Tunnel FET"] in _groups(obs).values(), _groups(obs)
    assert obs.outcome["returned"] == 8 and after["cluster_count"] == 8
    assert "kg_generation_catchup_settled" in obs.event_kinds()


def expect_fresh(obs: Observation) -> None:
    _assert_published(obs, merged_by_llm=False)
    assert obs.calls("unified_kg.checkpoint_clear") and not obs.calls("unified_kg.checkpoint_gc")
    assert [log["message"] for log in obs.logs if log["level"] == "ERROR"] == [
        f"unified-KG merge-review adjudication failed for {obs.notebook_id}; "
        "proceeding without it"]
    assert not obs.progress


def expect_maintenance_failures(obs: Observation) -> None:
    """Checkpoint GC, the final scratch cleanup and the claim release all fail:
    the rebuild still publishes; the release falls back to the TTL."""
    assert obs.outcome["returned"] == 7
    warnings = [log["message"] for log in obs.logs if log["level"] == "WARNING"]
    nb = obs.notebook_id
    assert f"rebuild checkpoint GC/clear 失败 for {nb}" in warnings
    assert any(w.startswith(f"rebuild scratch cleanup 失败 for {nb} run_id=") for w in warnings)
    assert any(w.startswith(f"rebuild 代际认领释放失败 for {nb} generation=1") for w in warnings)
    after = _state(obs)
    assert after["cluster_generation"] == 1 and after["derived_building_generation"] == 0
    assert obs.after["scratch"] != [{"n": 0}]
    assert _tail(obs) == DERIVED_TAIL


def expect_claim_refused(obs: Observation) -> None:
    assert obs.outcome["raised"] == "KgMaintenanceAlreadyRunning"
    assert obs.outcome["attrs"]["holder"] == "rebuild", obs.outcome
    held = _state(obs, "before")["derived_building_generation"]
    assert held and _state(obs) == _state(obs, "before")
    assert not obs.calls("self._stream_seed_reps")
    assert set(obs.changed_tables) <= {"sync_change_log"}, obs.changed_tables


def expect_stranded_debt(obs: Observation) -> None:
    """A crash after the last flip left the catch-up marker: the next forced rebuild
    settles the debt BEFORE it pre-reaps, then rebuilds; a large notebook re-arms
    the automatic index instead of building the viz synchronously."""
    calls = [e["call"] for e in obs.timeline if e.get("call") in {
        "self._settle_generation_catchup", "self._reap_stale_cluster_generations"}]
    assert calls[:2] == ["self._settle_generation_catchup",
                         "self._reap_stale_cluster_generations"], calls
    assert _is_debt(_state(obs, "before")["derived_catchup_from"])
    # This round's own anchor is settled right after its flip.
    assert _state(obs)["derived_catchup_from"] is None
    _assert_published(obs, merged_by_llm=True)
    assert obs.calls("scale_artifacts.rearm_auto_index")
    assert not obs.calls("scale_artifacts.build_viz")


def _assert_aborted(obs: Observation, raised: str) -> None:
    assert obs.outcome["raised"] == raised, obs.outcome
    before, after = _state(obs, "before"), _state(obs)
    assert after["cluster_generation"] == before["cluster_generation"]
    assert after.get("cluster_input_version") == before.get("cluster_input_version")
    _assert_claim_released(obs)
    assert not _tail(obs)


def expect_deleting(obs: Observation) -> None:
    _assert_aborted(obs, "NotebookDeletingAbortsMaintenanceError")
    assert not obs.calls("self._write_cluster_map_streamed")


def expect_prepare_failure(raising: str) -> Callable[[Observation], None]:
    """A step right after the claim fails (pre-reaping, or reading the
    checkpoint version): every step after the claim is inside the finally, so
    the error propagates and the claim is still released — otherwise the
    notebook stays locked for the whole TTL."""

    def expect(obs: Observation) -> None:
        assert obs.calls("unified_kg.claim_derived_generation")[0]["ret"]
        failed = [e for e in obs.calls(raising) if "raised" in e]
        assert failed, f"{raising} never failed"
        assert obs.outcome["message"] == "injected prepare failure", obs.outcome
        _assert_aborted(obs, "RuntimeError")
        assert obs.calls("unified_kg.release_derived_claim")
        assert not obs.calls("self._stream_seed_reps")
    return expect


def expect_preempted_pending(obs: Observation) -> None:
    assert obs.outcome["raised"] == "KgDerivedGenerationPreempted"
    assert obs.events()[-1]["stage"] == "pending_refresh"
    # The refresh never ran: the pending queue is the previous one (none here).
    assert obs.after["pending"] == obs.before["pending"]
    assert not _tail(obs)
    assert obs.after["scratch"] == [{"n": 0}]


def expect_flip_lost(obs: Observation) -> None:
    assert obs.outcome["raised"] == "KgDerivedGenerationPreempted"
    assert obs.events()[-1]["stage"] == "flip"
    assert _state(obs)["cluster_input_version"] in (None, "")
    _assert_claim_released(obs)
    assert not _tail(obs)


def expect_tail_failures(obs: Observation) -> None:
    _assert_published(obs, merged_by_llm=True)
    assert obs.event_kinds()[-3:] == ["canonical_relations_rebuild_failed",
                                      "mention_bridge_rebuild_failed",
                                      "communities_rebuild_failed"], obs.event_kinds()
    errors = [log for log in obs.logs if log["level"] == "ERROR"]
    assert [e["message"] for e in errors] == [
        f"maybe_auto_index failed after rebuild for {obs.notebook_id}"]


def expect_empty(obs: Observation) -> None:
    assert obs.outcome["returned"] == 0
    after = _state(obs)
    assert after["cluster_generation"] == 1 and after["cluster_count"] == 0
    assert after["derived_building_generation"] == 0
    assert not obs.after["published"] and not obs.after["pending"]
    assert not obs.llm
    assert "concept: descriptions skipped" in _stages(obs)


def expect_empty_again(obs: Observation) -> None:
    """Inputs unchanged but no clusters exist: the skip gate needs clusters, so
    an unforced refresh of an empty graph still runs (and claims) for real."""
    assert _state(obs, "before")["cluster_input_version"] == _state(obs)["cluster_input_version"]
    assert obs.calls("unified_kg.claim_derived_generation")
    assert obs.outcome["returned"] == 0
    assert _state(obs)["cluster_generation"] > _state(obs, "before")["cluster_generation"]
    assert _tail(obs) == DERIVED_TAIL


def expect_missing(obs: Observation) -> None:
    assert obs.outcome["raised"] == "KeyError"
    assert not obs.calls("unified_kg.claim_derived_generation")
    assert set(obs.changed_tables) <= {"sync_change_log"}, obs.changed_tables


def expect_llm_unconfigured(obs: Observation) -> None:
    _assert_published(obs, merged_by_llm=False)
    assert not obs.llm
    stages = _stages(obs)
    assert not any("merge-review" in s for s in stages)
    assert "concept: descriptions skipped" in stages
    assert not [r for r in obs.after["published"] if r["canonical_description"]]


# ------------------------------------------------------------------- installs


def _after_last_type(rig: Rig, action: Callable[[], None]) -> None:
    """Run ``action`` right after the last per-type cluster write (outside any
    transaction, before the pending refresh)."""

    def after(result, notebook_id, object_type, *args, **kwargs):
        if object_type == "procedure":
            action()

    rig.hooks.after["self._write_cluster_map_streamed"] = after


def install_live_changes(rig: Rig) -> None:
    nb = rig.notebook_id

    def action() -> None:
        # The world was rebuilt twice, so the library publishes concept seed
        # space 2: a decision landing now is minted there (its keys and
        # canonical ids squashed), as is an incremental placement.
        rig.sql("INSERT INTO concept_merge_candidates (id,notebook_id,canonical_a,canonical_b,"
                "seed_a,seed_b,score,status,seed_version,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("mc-live-rejected", nb, "K-finfet", "K-mostransistor", "finfet",
                 "mostransistor", 0.86,
                 "rejected", 2, REBUILD_NOW, REBUILD_NOW))
        lifecycle = rig.lifecycle
        lifecycle.store_kg(nb, "s3", [_obj("c-tfet", "concept", "Tunnel FET", "s3",
                                           "tunnel FET leaks less")], [])
        (oid,) = [o["id"] for o in rig.query(
            "SELECT id, payload FROM knowledge_objects WHERE notebook_id=? AND source_id=?",
            (nb, "s3"))]
        lifecycle.append_clusters(nb, [{"canonical_id": "K-tunnelfet", "member_object_id": oid,
                                        "canonical_name": "Tunnel FET"}], object_type="concept")

    _after_last_type(rig, action)


def _raise(message: str) -> Callable:
    def replace(real, *args, **kwargs):
        raise RuntimeError(message)
    return replace


def install_skip_derived_fail(rig: Rig) -> None:
    for name in ("rebuild_canonical_relations", "rebuild_mention_bridge", "rebuild_communities"):
        rig.hooks.replace[f"self.{name}"] = _raise("injected derived failure")


def install_tail_failures(rig: Rig) -> None:
    install_skip_derived_fail(rig)
    rig.hooks.replace["scale_artifacts.maybe_auto_index"] = _raise("injected index failure")


def install_debt(rig: Rig) -> None:
    rig.sql("UPDATE unified_kg_state SET derived_catchup_from=?, cluster_code_version='' "
            "WHERE notebook_id=?", (DEBT_TS, rig.notebook_id))


def install_debt_marker(rig: Rig) -> None:
    rig.sql("UPDATE unified_kg_state SET derived_catchup_from=? WHERE notebook_id=?",
            (DEBT_TS, rig.notebook_id))


def install_fresh_review_failure(rig: Rig) -> None:
    def replace(real, notebook_id, version, stage, *args, **kwargs):
        if stage == "merge_review":
            raise RuntimeError("injected checkpoint read failure")
        return real(notebook_id, version, stage, *args, **kwargs)

    rig.hooks.replace["unified_kg.checkpoint_load"] = replace


def install_maintenance_failures(rig: Rig) -> None:
    rig.hooks.replace["unified_kg.checkpoint_gc"] = _raise("injected gc failure")
    armed = {"flip": False}

    def after_flip(result, *args, **kwargs):
        armed["flip"] = True

    def scratch(real, *args, **kwargs):
        if armed["flip"]:
            raise RuntimeError("injected scratch cleanup failure")
        return real(*args, **kwargs)

    rig.hooks.after["unified_kg.flip_cluster_generation"] = after_flip
    rig.hooks.replace["unified_kg.clear_scratch_run"] = scratch
    rig.hooks.replace["unified_kg.release_derived_claim"] = _raise("injected release failure")


def install_claim_held(rig: Rig) -> None:
    with rig.repo._runtime.database.write() as db:
        assert rig.repo._runtime.unified_kg.claim_derived_generation(
            db, rig.notebook_id, ttl_seconds=3600) is not None


def install_deleting(rig: Rig) -> None:
    rig.hooks.replace["self._notebook_deleting"] = lambda real, notebook_id: True


def install_reap_failure(rig: Rig) -> None:
    rig.hooks.replace["self._reap_stale_cluster_generations"] = _raise(
        "injected prepare failure")


def install_checkpoint_version_failure(rig: Rig) -> None:
    def replace(real, notebook_id, **kwargs):
        if kwargs.get("exclude_emb_count"):
            raise RuntimeError("injected prepare failure")
        return real(notebook_id, **kwargs)

    rig.hooks.replace["self._cluster_input_version"] = replace


def install_preempt_before_pending(rig: Rig) -> None:
    _after_last_type(rig, lambda: rig.sql(
        "UPDATE unified_kg_state SET derived_building_generation=99 WHERE notebook_id=?",
        (rig.notebook_id,)))


def install_flip_lost(rig: Rig) -> None:
    def before(db, notebook_id, **kwargs):
        db.execute("UPDATE unified_kg_state SET cluster_generation=cluster_generation+100 "
                   "WHERE notebook_id=" + rig.backend.placeholder, (notebook_id,))

    rig.hooks.before["unified_kg.flip_cluster_generation"] = before


SCENARIOS = (
    Scenario("first_rebuild", kg_world, expect_first_rebuild),
    Scenario("skip_unchanged", kg_world_rebuilt(), expect_skip, progress="raise"),
    Scenario("skip_settles_debt_and_stamps", kg_world_rebuilt(), expect_skip_settles_debt,
             install=install_debt),
    Scenario("skip_derived_steps_fail", kg_world_rebuilt(), expect_skip_derived_fail,
             install=install_skip_derived_fail),
    Scenario("forced_third_generation", kg_world_rebuilt(2), expect_forced_second,
             force=True, install=install_live_changes),
    Scenario("fresh_with_review_failure", kg_world_rebuilt(), expect_fresh, fresh=True,
             progress=None, install=install_fresh_review_failure),
    Scenario("maintenance_failures", kg_world, expect_maintenance_failures, force=True,
             install=install_maintenance_failures),
    Scenario("claim_refused", kg_world, expect_claim_refused, force=True,
             install=install_claim_held),
    Scenario("stranded_debt_forced", kg_world_rebuilt(2), expect_stranded_debt, force=True,
             install=install_debt_marker, env={"VIZ_SYNC_BUILD_MAX_OBJECTS": "1"}),
    Scenario("notebook_deleting", kg_world, expect_deleting, force=True,
             install=install_deleting),
    Scenario("pre_reaping_fails", kg_world_rebuilt(), force=True,
             expect=expect_prepare_failure("self._reap_stale_cluster_generations"),
             install=install_reap_failure),
    Scenario("checkpoint_version_read_fails", kg_world, force=True,
             expect=expect_prepare_failure("self._cluster_input_version"),
             install=install_checkpoint_version_failure),
    Scenario("preempted_before_pending_refresh", kg_world, expect_preempted_pending,
             force=True, install=install_preempt_before_pending),
    Scenario("flip_lost", kg_world, expect_flip_lost, force=True, install=install_flip_lost),
    Scenario("derived_tail_failures", kg_world, expect_tail_failures, force=True,
             install=install_tail_failures),
    Scenario("empty_graph", empty_world, expect_empty, models=frozenset()),
    Scenario("empty_graph_rebuilt_again", empty_world_rebuilt, expect_empty_again,
             models=frozenset()),
    Scenario("missing_notebook", missing_world, expect_missing),
    Scenario("llm_unconfigured", kg_world, expect_llm_unconfigured, force=True,
             models=frozenset()),
)
