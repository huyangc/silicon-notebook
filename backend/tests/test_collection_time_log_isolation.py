"""Request logs of an app imported at collection time stay out of the checkout.

``import app.main`` runs the module-level ``app = create_app()``, and
``create_app`` binds the ``requests`` channel's ``EventLogger`` to the event
log directory resolved at that moment. A test module that imports the app at
module level therefore fixes that directory during collection, before the
per-test isolation fixture in ``conftest.py`` has set ``EVENT_LOG_DIR``; every
later request through that app object, from any test in the same worker, then
appends to whatever directory collection saw. With no process-level default
that was the checkout's ``.local/logs``, which made tests asserting the
checkout's logs stay untouched (the reasoning-loop characterization) fail
whenever such a request ran concurrently in another worker.

This module imports the app at collection time on purpose, so it holds for
any worker that collects it: the requests it logs must land outside the
checkout.
"""
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.event_logging import EventLogger
from app.main import app

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_a_collection_time_app_logs_requests_outside_the_checkout(monkeypatch):
    targets: list[Path] = []
    resolve_target = EventLogger._target_path_for_day

    def recording_target(self, day):
        target = resolve_target(self, day)
        if self.channel == "requests":
            targets.append(target)
        return target

    monkeypatch.setattr(EventLogger, "_target_path_for_day", recording_target)

    response = TestClient(app).get("/api/ready")

    assert response.status_code == 200
    assert targets, "the request was not logged at all"
    inside = [t for t in targets if t.resolve().is_relative_to(_REPO_ROOT)]
    assert inside == []
