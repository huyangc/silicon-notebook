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

# Upper bound on containers (mappings, sequences, records) inspected per port
# call; atoms are never pushed or counted.  Measured shapes stay far below it:
# 10,000 knowledge hits with 10 evidence rows each is about 150,000 containers,
# 60 hub concepts with 3,000 evidence rows each about 180,000.  Reaching it does
# NOT truncate the record: the call is marked overflowed and taking the record
# (``RetrievalSourceLog.source_ids``) raises, which fails the planning or
# generation — the same rule as a failed Memory lookup.  A silently partial
# record would under-count what the page may carry.
_MAX_CONTAINERS = 5_000_000
_ATOMS = (str, bytes, bytearray, int, float, bool, type(None))


class RetrievalRecordOverflow(RuntimeError):
    """A retrieval result was too large to read completely; the Memory record
    of this run cannot be trusted, so the run fails instead of under-counting."""


def _containers(values: Any) -> list[Any]:
    return [value for value in values if not isinstance(value, _ATOMS)]


def collect_source_ids(value: Any, out: set[str]) -> bool:
    """Add every non-empty ``source_id`` found in ``value`` to ``out``; return
    False when the value was too large to read completely.

    Reads mappings (key ``source_id``), dataclass instances and pydantic models
    (field or attribute ``source_id``), and walks lists, tuples, sets and the
    fields / values of the above.  Anything else is not entered."""
    if isinstance(value, _ATOMS):
        return True
    stack = [value]
    seen: set[int] = set()
    visited = 0
    while stack:
        if visited >= _MAX_CONTAINERS:
            return False
        item = stack.pop()
        marker = id(item)
        if marker in seen:
            continue
        seen.add(marker)
        visited += 1
        if isinstance(item, Mapping):
            source_id = item.get("source_id")
            if isinstance(source_id, str) and source_id:
                out.add(source_id)
            stack.extend(_containers(item.values()))
        elif isinstance(item, (list, tuple, set, frozenset)):
            stack.extend(_containers(item))
        elif dataclasses.is_dataclass(item) and not isinstance(item, type):
            source_id = getattr(item, "source_id", None)
            if isinstance(source_id, str) and source_id:
                out.add(source_id)
            stack.extend(_containers(
                getattr(item, f.name, None) for f in dataclasses.fields(item)
            ))
        elif hasattr(type(item), "model_fields"):
            source_id = getattr(item, "source_id", None)
            if isinstance(source_id, str) and source_id:
                out.add(source_id)
            stack.extend(_containers(
                getattr(item, name, None) for name in type(item).model_fields
            ))
    return True


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
        self._overflowed = False

    def watch(self, port: Any) -> Any:
        """``port`` with every call's result recorded."""
        return _Watched(port, self)

    def watch_call(self, function: Callable[..., Any]) -> Callable[..., Any]:
        """``function`` with its result recorded."""
        def recorded(*args: Any, **kwargs: Any) -> Any:
            result = function(*args, **kwargs)
            self.note(result)
            return result

        recorded.__watched_log__ = self  # type: ignore[attr-defined]
        return recorded

    def watches(self, value: Any) -> bool:
        """Whether ``value`` (a port or a callable) records into this log."""
        if isinstance(value, _Watched):
            return object.__getattribute__(value, "_log") is self
        return getattr(value, "__watched_log__", None) is self

    def note(self, value: Any) -> None:
        # Never raises: the watched call's result must reach its caller (which
        # may swallow retrieval errors).  An overflow is kept and reported
        # when the record is taken.
        found: set[str] = set()
        complete = collect_source_ids(value, found)
        with self._lock:
            self._source_ids.update(found)
            if not complete:
                self._overflowed = True

    def source_ids(self) -> list[str]:
        """Every recorded source id.  Raises ``RetrievalRecordOverflow`` when
        a result could not be read completely."""
        with self._lock:
            if self._overflowed:
                raise RetrievalRecordOverflow(
                    "a retrieval result was too large to record completely"
                )
            return sorted(self._source_ids)
