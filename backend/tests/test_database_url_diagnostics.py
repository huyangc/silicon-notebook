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


def test_postgres_database_status_is_credential_free():
    postgres = database_status(
        "postgresql://secret-user:secret-password@db.example:5432/notebook"
        "?access_token=secret"
    )

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
    ({"database_url": SECRET_URL},),
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


# 说明(移除 test_diag_base_report_redacts_settings_database_url):该用例曾断言
# diag_base_report 会加载 Settings/SQLiteRepository 并打印脱敏后的 DATABASE_URL。
# 那是 #337 fork 时 diag_base_report 的旧形态;#319「加固生产 DFX 诊断」把它重构成了
# 纯元数据模式——见 scripts/diag_base_report.py 的模块 docstring:「No application
# module, repository, migration, model, or retrieval path is loaded」,且从不输出
# database_url。合入后 #319 的实现胜出,该用例针对的代码路径(module.Settings /
# module.SQLiteRepository / 打印 database_url)已不存在,它守护的凭据泄漏面亦随之消失,
# 故移除。env 侧的 DATABASE_URL 脱敏仍由下方 test_diag_slow_redacts_database_url_from_env
# 守着(diag_slow 确实读并回显 .env 的 DATABASE_URL)。


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
