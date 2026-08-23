"""EXTENSIONS_CONFIG settings surface and verification-entrypoint isolation.

Discovery/load/capability-merge behavior (app.extensions.discovery) lands in
a later task; this module currently only covers the T1 configuration slot.
"""
from __future__ import annotations

from pathlib import Path

from app.core.config import Settings


_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_extensions_config_defaults_to_empty_and_anchors_relative_paths(
    monkeypatch, tmp_path
):
    import app.core.config as config_module

    assert Settings(_env_file=None).extensions_config == ""

    monkeypatch.setattr(config_module, "_ROOT_DIR", tmp_path)
    relative = Settings(_env_file=None, extensions_config="extensions.toml")
    assert relative.extensions_config == str(tmp_path / "extensions.toml")
    assert Path(relative.extensions_config).is_absolute()

    absolute_path = str(tmp_path / "abs" / "extensions.toml")
    absolute = Settings(_env_file=None, extensions_config=absolute_path)
    assert absolute.extensions_config == absolute_path


def test_verification_entrypoints_clear_extensions_config():
    check_sh = (_REPO_ROOT / "scripts" / "check.sh").read_text(encoding="utf-8")
    assert 'export EXTENSIONS_CONFIG=""' in check_sh

    conftest = (_REPO_ROOT / "backend" / "tests" / "conftest.py").read_text(
        encoding="utf-8"
    )
    assert 'os.environ["EXTENSIONS_CONFIG"] = ""' in conftest
