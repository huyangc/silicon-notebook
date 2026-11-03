"""Bounded single-worker, fire-and-forget local activity persistence."""
import logging
import queue
import threading
import weakref

_log = logging.getLogger("silicon_notebook.collaboration")
_writers: weakref.WeakSet = weakref.WeakSet()
_writers_lock = threading.Lock()


class NotebookActivityWriter:
    def __init__(self, store, capacity: int):
        self._store = store
        self._queue: queue.Queue = queue.Queue(maxsize=capacity)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._closed = False
        with _writers_lock:
            _writers.add(self)

    def submit(self, activity: dict) -> bool:
        with self._lock:
            if self._closed:
                return False
            try:
                self._queue.put_nowait(dict(activity))
            except queue.Full:
                _log.warning("collaboration_activity_queue_full")
                return False
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="collaboration-activity", daemon=True)
                self._thread.start()
            return True

    def _run(self) -> None:
        while True:
            activity = self._queue.get()
            try:
                if activity is None:
                    return
                try:
                    self._store.insert_activity(activity)
                except Exception:
                    # Do not log payloads, identities, paths or exception text.
                    _log.warning("collaboration_activity_insert_failed")
            finally:
                self._queue.task_done()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop_locked()

    def _stop_locked(self) -> None:
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join()
            self._thread = None


def _drain_activity_writers_for_tests() -> None:
    """Shared teardown includes every writer, including route-owned runtimes."""
    with _writers_lock:
        writers = tuple(_writers)
    for writer in writers:
        with writer._lock:
            # Stop every idle worker as well as drain pending writes. A
            # module-fixture runtime may submit again in its next test.
            writer._stop_locked()
