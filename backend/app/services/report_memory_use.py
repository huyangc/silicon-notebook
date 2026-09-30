"""Which sources a report run was handed by retrieval (M4 disclosure record).

Every prompt a report sends that is built from retrieved evidence — the outline
planner's corpus map and coverage probes, the section deep-dive agent's
planning and reflection turns, the report-wide synthesis payload, the section
drafting context — is assembled from what the engine's retrieval and evidence
ports return.  ``RetrievalSourceLog.watch`` wraps those ports for one engine
and records the ``source_id`` of every value they return; the engine maps the
recorded sources to the author's Memory (``memory_sources_for_source_ids``)
and stores the result on the report, so share disclosure counts Memory whose
content may have reached ANY prompt, cited or not.

What is recorded is what retrieval handed the run, a superset of what the
prompts finally carried (a candidate can be dropped before rendering).  That
is the safe direction for a disclosure that says "may contain".

The wrapper is transparent: attribute reads and ``getattr(port, name, None)``
probes go to the wrapped port on every access (so a replaced method is
honoured), non-callable attributes pass through unchanged, and a call's
result is returned untouched after it has been read.
"""
from __future__ import annotations

import dataclasses
import threading
from collections.abc import Mapping
from typing import Any, Callable

# Upper bound on values inspected per port call.  Retrieval results are lists
# of at most a few thousand hits with small payloads; the bound only stops a
# pathological structure from turning the record into real work.
_MAX_NODES = 1_000_000
_ATOMS = (str, bytes, bytearray, int, float, bool, type(None))


def collect_source_ids(value: Any, out: set[str]) -> None:
    """Add every non-empty ``source_id`` found in ``value`` to ``out``.

    Reads mappings (key ``source_id``), dataclass instances and pydantic models
    (field or attribute ``source_id``), and walks lists, tuples, sets and the
    fields / values of the above.  Anything else is not entered."""
    stack = [value]
    seen: set[int] = set()
    visited = 0
    while stack and visited < _MAX_NODES:
        item = stack.pop()
        visited += 1
        if isinstance(item, _ATOMS):
            continue
        marker = id(item)
        if marker in seen:
            continue
        seen.add(marker)
        if isinstance(item, Mapping):
            source_id = item.get("source_id")
            if isinstance(source_id, str) and source_id:
                out.add(source_id)
            stack.extend(item.values())
        elif isinstance(item, (list, tuple, set, frozenset)):
            stack.extend(item)
        elif dataclasses.is_dataclass(item) and not isinstance(item, type):
            source_id = getattr(item, "source_id", None)
            if isinstance(source_id, str) and source_id:
                out.add(source_id)
            stack.extend(getattr(item, f.name, None) for f in dataclasses.fields(item))
        elif hasattr(type(item), "model_fields"):
            source_id = getattr(item, "source_id", None)
            if isinstance(source_id, str) and source_id:
                out.add(source_id)
            stack.extend(getattr(item, name, None) for name in type(item).model_fields)


class _Watched:
    """Transparent proxy over one port; see the module docstring."""

    __slots__ = ("_inner", "_log")

    def __init__(self, inner: Any, log: "RetrievalSourceLog") -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_log", log)

    def __getattr__(self, name: str) -> Any:
        inner = self._inner
        value = getattr(inner, name)
        if not callable(value):
            return value
        log = self._log

        def recorded(*args: Any, **kwargs: Any) -> Any:
            result = value(*args, **kwargs)
            log.note(result)
            return result

        recorded.__watched_restore__ = (  # type: ignore[attr-defined]
            name, name in getattr(inner, "__dict__", {}), value,
        )
        return recorded

    def __setattr__(self, name: str, value: Any) -> None:
        # Writes go to the port itself, exactly as before it was watched (a
        # test replaces a port method through whichever engine holds it).
        # Writing back a wrapper this proxy handed out (a monkeypatch undo)
        # restores what that wrapper wrapped instead of leaving it behind.
        inner = self._inner
        restore = getattr(value, "__watched_restore__", None)
        if restore is not None and restore[0] == name:
            _name, was_instance_attribute, original = restore
            if was_instance_attribute:
                setattr(inner, name, original)
            elif name in getattr(inner, "__dict__", {}):
                delattr(inner, name)
            return
        setattr(inner, name, value)

    def __delattr__(self, name: str) -> None:
        delattr(self._inner, name)


class RetrievalSourceLog:
    """The sources one report engine was handed by retrieval.  Thread-safe:
    sections retrieve in parallel."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._source_ids: set[str] = set()

    def watch(self, port: Any) -> Any:
        """``port`` with every call's result recorded."""
        return _Watched(port, self)

    def watch_call(self, function: Callable[..., Any]) -> Callable[..., Any]:
        """``function`` with its result recorded."""
        def recorded(*args: Any, **kwargs: Any) -> Any:
            result = function(*args, **kwargs)
            self.note(result)
            return result

        return recorded

    def note(self, value: Any) -> None:
        found: set[str] = set()
        collect_source_ids(value, found)
        if found:
            with self._lock:
                self._source_ids.update(found)

    def source_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._source_ids)
