"""The deployment starter must work with both launch scripts and Settings."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from dotenv import dotenv_values

from app.core.config import Settings


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / ".env.example"


def test_env_example_has_the_same_values_in_shell_and_dotenv():
    expected = dict(dotenv_values(EXAMPLE, interpolate=False))
    # dev.sh/prod.sh source .env. This catches bare multi-word values and
    # empty assignments whose inline comments become values in dotenv.
    result = subprocess.run(
        [
            "bash", "--noprofile", "--norc", "-c",
            'set -eu; set -a; source "$1"; "$2" -c '
            "'import json, os, sys; "
            "print(json.dumps({key: os.environ[key] for key in sys.argv[1:]}))' "
            '"${@:3}"',
            "env-example", str(EXAMPLE), sys.executable, *expected,
        ],
        env={"PATH": os.defpath},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected


def test_env_example_uses_postgres_deployment_and_explicit_model_registry():
    # No developer .env, ambient service configuration, or network is used.
    with patch.dict(os.environ, {}, clear=True):
        defaults = Settings(_env_file=None)
        example = Settings(_env_file=EXAMPLE)
    assert example.model_services_config == str(ROOT / ".local/model-services.toml")
    assert example.database_url.startswith("postgresql://")
    assert defaults.database_url.startswith("sqlite:///")
    assert example.model_dump(exclude={"model_services_config", "database_url"}) == (
        defaults.model_dump(exclude={"model_services_config", "database_url"})
    )


def test_advanced_overrides_still_work_without_being_in_the_starter():
    with patch.dict(os.environ, {
        "PPR_DAMPING": "0.7",
        "ASK_PLUGIN_ENGINE_PROMPT_MAX_CHARS": "64000",
    }, clear=True):
        settings = Settings(_env_file=None)
    assert settings.ppr_damping == 0.7
    assert settings.ask_plugin_engine_prompt_max_chars == 64000
