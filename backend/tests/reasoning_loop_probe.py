"""Observation rig for ``ReasoningRetriever.run`` (reflect loop characterization).

Everything here is a recorder, never a behaviour change: a real SQLite
repository, real retrieval / experience-library / profile ports, and a
scripted chat model. What the rig records is what the loop exchanges with the
outside world, at the level the loop itself works at:

* the trace (and whether ``on_step`` streamed exactly those step objects);
* every model call: workload, roles, the ``messages`` bytes (SHA-256 + size),
  the schema hint (SHA-256) and the call kwargs — the model-visible input;
* the ``candidates_summary`` each reflect turn was given, verbatim — the text
  the loop itself assembles, so a reviewer can read a diff of it;
* the semantic identity of every port call (port, method, normalised
  arguments) on the retrieval port, the experience library (this is where
  ``note_adopted`` shows up) and the profile store, plus the plugin host's
  calls (recorded by the fake host itself).

SQL is deliberately NOT recorded: the loop reaches storage only through those
ports, so how a port spells or orders its statements is the store's business,
and pinning it would make every legitimate store change re-record this file.

Port calls are split by thread (see ``Probe.split``): the run's own thread —
where the reflect loop and the close execute — as an exact ordered sequence;
the first round's pool workers as a multiset.

Determinism across runs and platforms: row ids come from a counter instead of
``uuid4`` while a scenario seeds and runs; vector scores are projected out
(``SCORE_KEYS``); the process-wide caches the run reads
(``_EXPERIENCE_CACHE`` and every ``knowledge_counts_cache`` memo) are cleared
and every scenario gets a fresh repository. No wall clock feeds a recorded
value: ``duration_ms`` is not projected, and the only clock-dependent branches
(chunk rerank deadline, plugin timeout) are not exercised by any scenario.
"""
from __future__ import annotations

import collections
import contextlib
import dataclasses
import hashlib
import json
import sys
import threading
import uuid
from enum import Enum
from typing import Any, Dict, List, Optional


class Probe:
    """One scenario's recorder; ``active`` gates every channel."""

    def __init__(self) -> None:
        self.active = False
        self.main_ident = threading.get_ident()
        self.ports: List[tuple] = []
        self.model_calls: List[dict] = []
        self.reflect_inputs: List[dict] = []
        self.embeddings: collections.Counter = collections.Counter()
        self._lock = threading.Lock()

    def note_port(self, label: str) -> None:
        if self.active:
            with self._lock:
                self.ports.append((threading.get_ident(), label))

    def split(self, rows: List[tuple]) -> Dict[str, Any]:
        """The run's own thread as an ordered sequence; the first round's pool
        workers as a multiset (sorted list) — their order is scheduling."""
        main = [value for ident, value in rows if ident == self.main_ident]
        workers = sorted(value for ident, value in rows if ident != self.main_ident)
        return {"main_thread": main, "worker_threads": workers}


_CURRENT: List[Optional[Probe]] = [None]


def current_probe() -> Optional[Probe]:
    return _CURRENT[0]


@contextlib.contextmanager
def probing(probe: Probe):
    _CURRENT[0] = probe
    try:
        yield probe
    finally:
        _CURRENT[0] = None


@contextlib.contextmanager
def deterministic_ids():
    """``uuid4`` → a counter, both as ``uuid.uuid4`` and as every ``app.*``
    module's own ``uuid4`` binding (``from uuid import uuid4``)."""
    real = uuid.uuid4
    counter = [0]
    lock = threading.Lock()

    def fake_uuid4():
        with lock:
            counter[0] += 1
            return uuid.UUID(int=counter[0])

    patched = []
    uuid.uuid4 = fake_uuid4
    for name, module in list(sys.modules.items()):
        if not name.startswith("app.") or module is None:
            continue
        if getattr(module, "uuid4", None) is real:
            module.uuid4 = fake_uuid4
            patched.append(module)
    try:
        yield
    finally:
        uuid.uuid4 = real
        # every binding, including modules first imported inside the block
        for module in patched + list(sys.modules.values()):
            if module is not None and getattr(module, "uuid4", None) is fake_uuid4:
                module.uuid4 = real


#: Vector-derived scores. Their last bits follow the BLAS kernel (float32
#: ``matrix @ q``), so they are projected out; the ORDER they produce is kept
#: (every list keeps its order) and the fixture has no mathematical ties, so
#: that order is the same on every BLAS.
SCORE_KEYS = frozenset({"score", "relevance", "query_relevance"})


def canon(value: Any, *, depth: int = 0) -> Any:
    """A JSON projection that is stable across runs and platforms (sets
    sorted, objects flattened field by field, vector scores dropped)."""
    if depth > 12:
        return f"<{type(value).__name__}>"
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return round(value, 9)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): canon(v, depth=depth + 1) for k, v in value.items()
                if str(k) not in SCORE_KEYS}
    if isinstance(value, (list, tuple)):
        return [canon(v, depth=depth + 1) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted((canon(v, depth=depth + 1) for v in value), key=repr)
    if isinstance(value, threading.Event):
        return "<event>"
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: canon(getattr(value, field.name), depth=depth + 1)
            for field in dataclasses.fields(value)
            if field.name not in SCORE_KEYS
        }
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return canon(dump(), depth=depth + 1)
    if callable(value):
        return f"<callable {getattr(value, '__name__', type(value).__name__)}>"
    return f"<{type(value).__name__}>"


def port_label(prefix: str, name: str, args, kwargs) -> str:
    payload = {"args": canon(list(args)), "kwargs": canon(dict(kwargs))}
    return f"{prefix}.{name} " + json.dumps(
        payload, ensure_ascii=False, sort_keys=True)


class PortProbe:
    """Transparent proxy: every callable attribute reports one call.

    ``overrides`` replaces a method for one scenario (fabricated failure or
    shape); the call is still recorded under the same label.
    """

    def __init__(self, inner, prefix: str, overrides: Optional[dict] = None):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_prefix", prefix)
        object.__setattr__(self, "_overrides", dict(overrides or {}))

    def __getattr__(self, name):
        inner = object.__getattribute__(self, "_inner")
        overrides = object.__getattribute__(self, "_overrides")
        prefix = object.__getattribute__(self, "_prefix")
        if name in overrides:
            target = overrides[name]
            if isinstance(target, tuple) and target[0] == "wrap":
                target = target[1](getattr(inner, name))
        else:
            target = getattr(inner, name)
            if not callable(target):
                return target

        def call(*args, **kwargs):
            probe = current_probe()
            if probe is not None:
                probe.note_port(port_label(prefix, name, args, kwargs))
            return target(*args, **kwargs)

        return call

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_inner"), name, value)


def recorded_callable(target, label: str):
    """A port that is a bare callable (``source_generation``)."""

    def call(*args, **kwargs):
        probe = current_probe()
        if probe is not None:
            probe.note_port(port_label(label, "__call__", args, kwargs))
        return target(*args, **kwargs)

    return call


def sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


class ScriptedChat:
    """One chat client per workload; all share ``script``.

    Reflect payloads go through the production shape gate (the same two calls
    the deployment's adapter makes), so a scripted turn is a turn the model
    could actually send.
    """

    configured = True

    def __init__(self, workload: str, script: "ChatScript"):
        self.workload = workload
        self.script = script

    def chat_json(self, messages, schema_hint, **kwargs):
        probe = current_probe()
        if probe is not None and probe.active:
            probe.model_calls.append({
                "workload": self.workload,
                "messages_sha256": sha256(messages),
                "messages_bytes": len(json.dumps(
                    messages, ensure_ascii=False).encode()),
                "roles": [message.get("role") for message in messages],
                "schema_sha256": sha256(schema_hint),
                "kwargs": canon({
                    key: value for key, value in sorted(kwargs.items())
                }),
            })
        return self.script.answer(self.workload, messages, schema_hint)


class ChatScript:
    def __init__(self, *, plan: dict, reflects: List[Any], context: dict):
        self.plan = plan
        self.reflects = list(reflects)
        self.context = context

    def answer(self, workload, messages, schema_hint):
        from app.core.model_json import (
            ModelJsonRepairError,
            parse_model_json_object,
            validate_model_json_shape,
        )
        from app.services.model_work import MalformedModelResponse

        if "next_action" in schema_hint:
            prompt = messages[-1]["content"]
            item = (
                self.reflects.pop(0) if self.reflects
                else {"next_action": "answer", "sufficient": True}
            )
            if callable(item):
                item = item(self.context, prompt)
            if isinstance(item, Exception):
                raise item
            raw = item if isinstance(item, str) else json.dumps(
                item, ensure_ascii=False)
            try:
                parsed = parse_model_json_object(
                    raw, schema_hint, allow_repair=False)
                validate_model_json_shape(parsed.content, schema_hint)
            except ModelJsonRepairError as exc:
                raise MalformedModelResponse() from exc
            return parsed.content
        if "sub_queries" in schema_hint:
            return json.dumps(self.plan, ensure_ascii=False)
        return json.dumps({}, ensure_ascii=False)


class CountingEmbedder:
    """``FakeEmbedder`` plus a per-method call count on the probe."""

    configured = True

    def __init__(self, dim: int = 16):
        from app.services.embedding import FakeEmbedder

        self._inner = FakeEmbedder(dim=dim)
        self.dim = dim

    def embed_query(self, text):
        probe = current_probe()
        if probe is not None and probe.active:
            probe.embeddings["embed_query"] += 1
        return self._inner.embed_query(text)

    def embed_texts(self, texts):
        probe = current_probe()
        if probe is not None and probe.active:
            probe.embeddings["embed_texts"] += 1
        return self._inner.embed_texts(texts)


def trace_projection(steps) -> List[dict]:
    return [
        {
            "step_type": step.step_type,
            "summary": step.summary,
            "detail": canon(step.detail or {}),
        }
        for step in steps
    ]


def result_projection(result) -> Dict[str, Any]:
    """Every ``ReasoningResult`` field except the trace (recorded separately)."""
    out = {}
    for field in dataclasses.fields(result):
        if field.name == "trace":
            continue
        out[field.name] = canon(getattr(result, field.name))
    return out


def install_reflect_recorder(retriever, probe: Probe) -> None:
    original = retriever.reflect

    def recorded(question, candidates_summary, *args, **kwargs):
        probe.reflect_inputs.append({
            "candidates_summary": candidates_summary,
            "args": canon(list(args)),
            "kwargs": canon(kwargs),
        })
        return original(question, candidates_summary, *args, **kwargs)

    retriever.reflect = recorded

