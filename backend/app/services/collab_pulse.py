"""Bounded process-local collaboration hints; never reads or writes persistence."""
from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from app.core.config import Settings
from app.domain.collaboration import (
    PULSE_COALESCE_SECONDS,
    PULSE_KINDS,
    PULSE_PRESENCE_INTERVALS,
    PulseActor,
    PulseKind,
)


@dataclass
class _Change:
    rev: int
    kind: str
    actor: PulseActor
    actor_ids: set[str]
    at: str
    monotonic_at: float
    capability: str
    target: str


@dataclass
class _Entry:
    created_rev: int
    activities: deque[dict]
    dropped_rev: int = 0
    changes: OrderedDict[int, _Change] = field(default_factory=OrderedDict)
    latest: dict[str, _Change] = field(default_factory=dict)
    viewers: dict[str, tuple[PulseActor, float]] = field(default_factory=dict)


class CollabPulse:
    """One lock protects epoch/rev, LRU entries, coalescing and presence together.

    A coalesced update receives a new global revision. Its actors are retained
    so a later update by the viewer cannot hide a peer's earlier update.
    """

    def __init__(self, settings: Settings, *, clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], datetime] | None = None, epoch: str | None = None):
        self.settings = settings
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._epoch = epoch or uuid.uuid4().hex
        self._rev = 0
        self._evicted_rev = 0
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = threading.Lock()

    def cursor(self) -> str:
        with self._lock:
            return self._cursor()

    def _cursor(self) -> str:
        return f"{self._epoch}:{self._rev}"

    def _entry(self, notebook_id: str) -> _Entry:
        entry = self._entries.get(notebook_id)
        if entry is None:
            entry = _Entry(created_rev=self._rev, activities=deque(maxlen=self.settings.notebook_activity_panel_limit))
            self._entries[notebook_id] = entry
        self._entries.move_to_end(notebook_id)
        while len(self._entries) > self.settings.collab_pulse_max_notebooks:
            _, removed = self._entries.popitem(last=False)
            self._evicted_rev = max(self._evicted_rev, removed.dropped_rev,
                                    max(removed.changes, default=0))
        return entry

    def touch(self, notebook_id: str, kind: PulseKind, actor: PulseActor, *,
              capability: str = "", target: str = "", affected_kinds=(), activity: bool = True) -> None:
        kinds = affected_kinds or (kind,)
        if kind not in PULSE_KINDS or any(affected not in PULSE_KINDS for affected in kinds):
            raise ValueError("unknown collaboration kind")
        if kind == "none":
            return
        with self._lock:
            entry = self._entry(notebook_id)
            at = self._wall_clock().isoformat()
            for affected in kinds:
                if affected != "none":
                    self._touch(entry, affected, actor, at, capability, target)
            if activity:
                # Primary actions have their own bounded projection. Derived
                # hints may coalesce or evict without rewriting action identity.
                entry.activities.append({"id": self._cursor(), "kind": kind,
                                         "capability": capability, "actor_id": actor.user_id,
                                         "actor_name": actor.display_name, "target": target, "created_at": at})

    def _touch(self, entry: _Entry, kind: str, actor: PulseActor, at: str, capability: str, target: str) -> None:
        now = self._clock()
        previous = entry.latest.get(kind)
        actors = {actor.user_id}
        if previous is not None and now - previous.monotonic_at < PULSE_COALESCE_SECONDS:
            actors.update(previous.actor_ids)
            entry.changes.pop(previous.rev, None)
        self._rev += 1
        change = _Change(self._rev, kind, actor, actors, at, now, capability, target)
        entry.changes[change.rev] = change
        entry.latest[kind] = change
        while len(entry.changes) > self.settings.collab_pulse_ring_size:
            rev, removed = entry.changes.popitem(last=False)
            entry.dropped_rev = max(entry.dropped_rev, rev)
            if entry.latest.get(removed.kind) is removed:
                entry.latest.pop(removed.kind)

    def next_interval(self, pool_wait_ms: float = 0) -> int:
        slow, very_slow = (int(value) for value in self.settings.collab_pulse_slow_intervals.split(","))
        if pool_wait_ms >= self.settings.collab_pulse_pool_wait_very_slow_ms:
            return very_slow
        if pool_wait_ms >= self.settings.collab_pulse_pool_wait_slow_ms:
            return slow
        return self.settings.collab_pulse_interval_seconds

    def poll(self, notebook_id: str, actor: PulseActor, *, since: str,
             visible: bool, pool_wait_ms: float = 0) -> dict:
        with self._lock:
            # Evaluate loss before creating a heartbeat-only entry, preserving
            # the distinction between an evicted notebook and a cold notebook.
            entry = self._entries.get(notebook_id)
            reset, rev = self._reset(since, entry)
            interval = self.next_interval(pool_wait_ms)
            now = self._clock()
            entry = self._entry(notebook_id)
            entry.viewers = {user_id: value for user_id, value in entry.viewers.items() if value[1] > now}
            if visible:
                entry.viewers[actor.user_id] = (actor, now + PULSE_PRESENCE_INTERVALS * interval)
            else:
                entry.viewers.pop(actor.user_id, None)
            changes: dict[str, _Change] = {}
            if not reset:
                for change in entry.changes.values():
                    if change.rev > rev:
                        changes[change.kind] = change
                # Omit only when every recorded actor since this cursor is self.
                changes = {kind: change for kind, change in changes.items()
                           if any(item.rev > rev and item.kind == kind and item.actor_ids != {actor.user_id}
                                  for item in entry.changes.values())}
            writers = [value[0] for value in entry.viewers.values() if value[0].role != "reader"]
            return {"cursor": self._cursor(), "reset": reset,
                    "changes": [{"kind": change.kind, "actor_name": change.actor.display_name, "at": change.at}
                                for change in changes.values()],
                    "viewers": [{"user_id": writer.user_id, "display_name": writer.display_name, "role": writer.role}
                                for writer in sorted(writers, key=lambda writer: writer.user_id)],
                    "other_reader_count": sum(value[0].role == "reader" and user_id != actor.user_id
                                              for user_id, value in entry.viewers.items()),
                    "next_interval_s": interval}

    def _reset(self, since: str, entry: _Entry | None) -> tuple[bool, int]:
        try:
            epoch, raw_rev = since.rsplit(":", 1)
            if not raw_rev.isascii() or not raw_rev.isdecimal():
                return True, 0
            rev = int(raw_rev)
        except (ValueError, AttributeError):
            return True, 0
        reset = (epoch != self._epoch or rev > self._rev
                 or (rev < self._evicted_rev and (entry is None or entry.created_rev > rev))
                 or (entry is not None and rev < entry.dropped_rev))
        return reset, rev

    def recent_owner_changes(self, notebook_id: str, owner_id: str) -> list[dict]:
        """Owner's transient records complement persisted non-owner activity."""
        with self._lock:
            entry = self._entries.get(notebook_id)
            if entry is None:
                return []
            return [dict(record) for record in reversed(entry.activities) if record["actor_id"] == owner_id]
