"""Request logs of an app imported at collection time go to a temp directory.

``import app.main`` runs the module-level ``app = create_app()``, and
``create_app`` binds the ``requests`` channel's ``EventLogger`` to the event
log directory resolved at that moment. A test module that imports the app at
module level therefore fixes that directory during collection, before the
per-test isolation fixture in ``conftest.py`` has set ``EVENT_LOG_DIR``; every
later request through that app object, from any test in the same worker, then
appends to whatever directory collection saw. With no process-level default
that was the checkout's ``.local/logs`` (or a log directory exported by the
developer's shell or ``.env``), which made tests asserting the checkout's logs
stay untouched (the reasoning-loop characterization) fail whenever such a
request ran concurrently in another worker.

Both checks below judge against the system temp directory rather than "not
the checkout", so a developer's own log directory outside the repository does
not satisfy them either. The environment is read at module level, i.e. during
collection, so the first check still holds if the app import below is ever
moved into a function; keep that import at module level all the same, since
the second check only exercises a collection-time app because of it.

The last check holds ``DATABASE_URL`` and ``SILICON_NOTEBOOK_STORAGE_DIR`` to
the same rule: outside the per-test fixture their Settings defaults are the
checkout's database file and storage directory.
"""
import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.event_logging import EventLogger
from app.main import app

_LOG_ENV_NAMES = ("EVENT_LOG_DIR", "LLM_LOG_PATH")
#: What collection-time code (module-level apps, higher-scoped fixtures) sees.
_COLLECTION_LOG_ENV = {name: os.environ.get(name) for name in _LOG_ENV_NAMES}
#: The same window for the database and storage: falling back to the Settings
#: defaults would reach the checkout's ``.local/silicon_notebook.db`` and
#: ``.local/storage`` (a developer's real ones in the main checkout).
_COLLECTION_DATABASE_URL = os.environ.get("DATABASE_URL")
_COLLECTION_STORAGE_DIR = os.environ.get("SILICON_NOTEBOOK_STORAGE_DIR")
_SQLITE_PREFIX = "sqlite:///"


def _temp_root() -> Path:
    return Path(tempfile.gettempdir()).resolve()


def test_collection_time_log_settings_point_into_the_temp_dir():
    outside = {
        name: value
        for name, value in _COLLECTION_LOG_ENV.items()
        if not value or not Path(value).resolve().is_relative_to(_temp_root())
    }
    assert outside == {}


def test_a_collection_time_app_logs_requests_into_the_temp_dir(monkeypatch):
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
    outside = [t for t in targets if not t.resolve().is_relative_to(_temp_root())]
    assert outside == []


def test_collection_time_database_and_storage_point_into_the_temp_dir():
    database_url = _COLLECTION_DATABASE_URL or ""
    database_path = (
        database_url[len(_SQLITE_PREFIX):]
        if database_url.startswith(_SQLITE_PREFIX)
        else ""
    )
    outside = {
        name: value
        for name, value, path in (
            ("DATABASE_URL", database_url, database_path),
            ("SILICON_NOTEBOOK_STORAGE_DIR", _COLLECTION_STORAGE_DIR, _COLLECTION_STORAGE_DIR),
        )
        if not path or not Path(path).resolve().is_relative_to(_temp_root())
    }
    assert outside == {}
