import importlib.util
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.database_url import database_status


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
SECRET_URL = "mysql://redacted-user:redacted-password@db.example/notebook?access_token=redacted-token#fragment"
REDACTED_IDENTITY = "postgresql://db.example:5432/notebook"


def test_database_status_is_backend_neutral_and_credential_free(tmp_path):
    sqlite = database_status(f"sqlite:///{tmp_path / 'db.sqlite'}")
    postgres = database_status(
        "postgresql://secret-user:secret-password@db.example:5432/notebook"
        "?access_token=secret"
    )

    assert sqlite == f"database=sqlite path={tmp_path / 'db.sqlite'}"
    assert postgres == "database=postgresql host=db.example:5432 db=notebook"
    assert "secret" not in postgres


def _load_script(module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / f"{module_name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # diag scripts import their stdlib sibling ``diag_common`` (see scripts/diag.py),
    # which resolves only with scripts/ on sys.path — as it is when run as a script,
    # but not when this test loads the file by path. Add it just for exec_module.
    scripts_dir = str(SCRIPTS)
    added = scripts_dir not in sys.path
    if added:
        sys.path.insert(0, scripts_dir)
    try:
        spec.loader.exec_module(module)
    finally:
        if added:
            sys.path.remove(scripts_dir)
    return module


@pytest.mark.parametrize(
    "settings_kwargs",
    (
        {"database_url": SECRET_URL},
        {
            "database_url": "sqlite:///.local/silicon_notebook.db",
            "shadow_database_url": SECRET_URL,
        },
    ),
)
def test_settings_validation_errors_keep_database_url_secrets_out_of_strings(settings_kwargs):
    with pytest.raises(ValidationError) as captured:
        Settings(**settings_kwargs)

    diagnostics = (
        str(captured.value),
        repr(captured.value.errors()),
        captured.value.json(),
    )

    assert "unsupported database URL scheme" in diagnostics[0]
    assert "database_url" in diagnostics[0].lower()
    for diagnostic in diagnostics:
        assert SECRET_URL not in diagnostic
        assert "redacted-user" not in diagnostic
        assert "redacted-password" not in diagnostic
        assert "access_token=redacted-token" not in diagnostic
        assert "#fragment" not in diagnostic


def test_diag_base_report_redacts_settings_database_url(monkeypatch, capsys):
    module = _load_script("diag_base_report")

    class SettingsStub:
        database_url = "postgresql://redacted-user:redacted-password@db.example:5432/notebook?access_token=redacted-token"
        storage_dir = ".local/storage"

    class MaintenanceStub:
        def notebook_rows(self):
            return []

        def kg_object_counts_by_notebook(self):
            return {}

        def latest_done_report(self):
            return None

    class RepositoryStub:
        maintenance = MaintenanceStub()

    monkeypatch.setattr(module, "Settings", SettingsStub)
    monkeypatch.setattr(module, "SQLiteRepository", lambda _settings: RepositoryStub())
    monkeypatch.setattr(sys, "argv", ["diag_base_report.py"])

    module.main()

    diagnostic = capsys.readouterr().out
    assert "redacted-user" not in diagnostic
    assert "redacted-password" not in diagnostic
    assert "access_token=redacted-token" not in diagnostic
    assert REDACTED_IDENTITY in diagnostic


def test_diag_slow_redacts_database_url_from_env(tmp_path, capsys):
    module = _load_script("diag_slow")
    (tmp_path / ".env").write_text(
        "DATABASE_URL=postgresql://redacted-user:redacted-password@db.example:5432/notebook?access_token=redacted-token\n",
        encoding="utf-8",
    )

    module.report_env(str(tmp_path))

    diagnostic = capsys.readouterr().out
    assert "redacted-user" not in diagnostic
    assert "redacted-password" not in diagnostic
    assert "access_token=redacted-token" not in diagnostic
    assert REDACTED_IDENTITY in diagnostic
