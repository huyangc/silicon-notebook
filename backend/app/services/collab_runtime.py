"""Composition of process-local pulse, bounded history writes and pool pressure."""
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone

from app.domain.collaboration import PULSE_POOL_SAMPLE_SECONDS, PULSE_POOL_WINDOW_SECONDS, CollabMark, PulseActor
from app.services.collab_pulse import CollabPulse
from app.services.notebook_activity_writer import NotebookActivityWriter


class PoolWaitWindow:
    def __init__(self, read_stats, *, clock=time.monotonic):
        self._read_stats = read_stats
        self._clock = clock
        self._samples: deque = deque()
        self._lock = threading.Lock()

    def wait_ms(self) -> float:
        stats = self._read_stats()
        sample = (self._clock(), int(stats.get("requests_num", 0)), int(stats.get("requests_wait_ms", 0)))
        with self._lock:
            if not self._samples:
                self._samples.append(sample)
                return 0.0
            if sample[0] - self._samples[-1][0] > PULSE_POOL_WINDOW_SECONDS:
                # No observations establish when an old counter increase
                # occurred. Do not attribute a quiet hour to this window.
                self._samples.clear()
                self._samples.append(sample)
                return 0.0
            while len(self._samples) > 1 and self._samples[1][0] <= sample[0] - PULSE_POOL_WINDOW_SECONDS:
                self._samples.popleft()
            first = self._samples[0]
            if sample[1] < first[1] or sample[2] < first[2]:
                self._samples.clear()
                self._samples.append(sample)
                return 0.0
            requests = sample[1] - first[1]
            waiting = sample[2] - first[2]
            value = waiting / requests if requests else 0.0
            # One sample per second bounds memory independently of request
            # rate, retaining one anchor just before the ten-second window.
            if sample[0] - self._samples[-1][0] >= PULSE_POOL_SAMPLE_SECONDS:
                self._samples.append(sample)
            return value


class CollaborationRuntime:
    def __init__(self, settings, store, actors, database):
        self.settings = settings
        self.store = store
        self.actors = actors
        self.pulse = CollabPulse(settings)
        self.activity_writer = NotebookActivityWriter(store, settings.notebook_activity_queue_size)
        self.pool_wait = PoolWaitWindow(database.pool_wait_stats)

    def record(self, mark: CollabMark, kind, target: str, affected_kinds=()) -> None:
        self.pulse.touch(mark.notebook_id, kind, mark.actor, capability=mark.capability, target=target,
                         affected_kinds=affected_kinds, activity=mark.actor.user_id == mark.owner_id)
        # Multiple invalidation hints still describe one committed action.
        if kind != "none" and mark.actor.user_id != mark.owner_id:
            self.activity_writer.submit({
                "id": uuid.uuid4().hex, "notebook_id": mark.notebook_id, "kind": kind,
                "capability": mark.capability, "actor_id": mark.actor.user_id,
                "actor_name": mark.actor.display_name, "target": target,
                "created_at": datetime.now(timezone.utc).isoformat(),
            })

    def recent_activity(self, notebook_id: str, owner_id: str) -> dict:
        limit = self.settings.notebook_activity_panel_limit
        records = self.store.list_recent_activity(notebook_id, limit)
        records.extend(self.pulse.recent_owner_changes(notebook_id, owner_id))
        for record in records:
            record.setdefault("notebook_id", notebook_id)
        records.sort(key=lambda row: (datetime.fromisoformat(row["created_at"]).timestamp(), row["id"]), reverse=True)
        # Publicly documented bounded recent-activity projection, never a
        # truncation of user-authored input.
        return {"items": records[:limit]}

    def touch_for_job(self, notebook_id: str, kind, actor_id: str | None = None, actor_name: str | None = None, *, activity=True) -> None:
        from app.services.collab_jobs import current_job_actor

        captured = current_job_actor()
        if captured is not None and (actor_id is None or captured.user_id == actor_id):
            actor_id = captured.user_id
            if actor_name is None:
                actor_name = captured.display_name
        if not actor_id:
            return
        if actor_name is None:
            row = self.actors.find_user_by_id(actor_id)
            actor_name = (str(row.get("display_name") or "").strip() or str(row.get("username") or "").strip()) if row else ""
        # A derived-surface batch shares one actor resolution, including the
        # single PK recovery read for a durable job resumed after restart.
        kinds = (kind,) if isinstance(kind, str) else tuple(kind)
        if kinds:
            self.pulse.touch(notebook_id, kinds[0], PulseActor(actor_id, actor_name, "editor"),
                             affected_kinds=kinds, activity=activity)

    def close(self) -> None:
        self.activity_writer.close()
