import importlib.util
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
SECRET_URL = "mysql://redacted-user:redacted-password@db.example/notebook?access_token=redacted-token#fragment"
REDACTED_IDENTITY = "postgresql://db.example:5432/notebook"


def _load_script(module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / f"{module_name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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
