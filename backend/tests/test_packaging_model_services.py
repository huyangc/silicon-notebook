from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib


ROOT = Path(__file__).resolve().parents[2]
PACKAGING = ROOT / "packaging"

RETIRED_ENDPOINT_SETTINGS = (
    "OPENAI_COMPAT_BASE_URL",
    "OPENAI_COMPAT_API_KEY",
    "OPENAI_COMPAT_MODEL",
    "EMBED_BASE_URL",
    "EMBED_API_KEY",
    "EMBED_MODEL",
    "EMBED_CONCURRENCY",
    "RERANK_BASE_URL",
    "RERANK_API_KEY",
    "RERANK_MODEL",
    "RERANK_CONCURRENCY",
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_packaged_model_service_guidance_uses_system_toml_only():
    sources = {
        ".env.example": _text(ROOT / ".env.example"),
        "install.sh": _text(PACKAGING / "install.sh"),
        "start.sh": _text(PACKAGING / "start.sh"),
        "DEPLOY.md": _text(PACKAGING / "DEPLOY.md"),
    }

    for name, content in sources.items():
        for retired in RETIRED_ENDPOINT_SETTINGS:
            pattern = rf"(?<![A-Z0-9_]){re.escape(retired)}(?![A-Z0-9_])"
            assert not re.search(pattern, content), f"{name} still documents {retired}"

    assert "MODEL_SERVICES_CONFIG=.local/model-services.toml" in sources[".env.example"]
    guidance = "\n".join(
        (sources["install.sh"], sources["start.sh"], sources["DEPLOY.md"])
    )
    for required in (
        "model-services.example.toml",
        ".local/model-services.toml",
        "services",
        "bindings",
        "max_concurrency",
        "api_key_env",
        "MODEL_SERVICES_CONFIG",
    ):
        assert required in guidance
    assert "离线" in guidance


def test_release_bundle_includes_legacy_model_env_migration_helper():
    pack_script = _text(ROOT / "scripts" / "pack.sh")

    assert "scripts/migrate_legacy_model_env.py" in pack_script
    assert "chmod +x \"$STAGE/scripts/migrate_legacy_model_env.py\"" in pack_script


def test_packaged_migration_helper_runs_with_bundled_python_layout(tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "scripts").mkdir(parents=True)
    (bundle / "backend").mkdir()
    (bundle / ".local").mkdir()
    (bundle / ".venv" / "bin").mkdir(parents=True)
    shutil.copytree(ROOT / "backend" / "app", bundle / "backend" / "app")
    shutil.copy2(
        ROOT / "scripts" / "migrate_legacy_model_env.py",
        bundle / "scripts" / "migrate_legacy_model_env.py",
    )
    shutil.copy2(
        ROOT / "model-services.example.toml",
        bundle / "model-services.example.toml",
    )
    shutil.copy2(
        ROOT / "model-services.example.toml",
        bundle / ".local" / "model-services.toml",
    )
    bundled_python = bundle / ".venv" / "bin" / "python"
    bundled_python.symlink_to(sys.executable)
    legacy_env = """OPENAI_COMPAT_BASE_URL=https://llm.example/v1
OPENAI_COMPAT_API_KEY=legacy-secret
OPENAI_COMPAT_MODEL=legacy-model
REWRITE_LLM_MODEL=fast-model
KG_EXTRACT_WORKERS=2
KG_ASK_RESERVE=3
"""
    (bundle / ".env").write_text(legacy_env, encoding="utf-8")

    result = subprocess.run(
        [
            str(bundled_python),
            "scripts/migrate_legacy_model_env.py",
            "--env",
            ".env",
            "--apply",
        ],
        cwd=bundle,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    generated = tomllib.loads(
        (bundle / ".local" / "model-services.toml").read_text(encoding="utf-8")
    )
    assert generated["services"]["general"]["max_concurrency"] == 5
    assert generated["bindings"]["report_sufficiency"] == "rewrite"
    assert generated["bindings"]["knowhow_optimize"] == "rewrite"
    assert "legacy-secret" not in result.stdout + result.stderr
    backup = bundle / ".env.pre-system-models.bak"
    assert backup.read_text(
        encoding="utf-8"
    ) == legacy_env
    assert (bundle / ".env").stat().st_mode & 0o777 == 0o600
    assert backup.stat().st_mode & 0o777 == 0o600


def test_install_creates_model_service_config_once_and_preserves_it(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    shutil.copy2(PACKAGING / "install.sh", bundle / "install.sh")
    shutil.copy2(ROOT / ".env.example", bundle / ".env.example")
    shutil.copy2(
        ROOT / "model-services.example.toml",
        bundle / "model-services.example.toml",
    )

    (bundle / "backend").mkdir()
    (bundle / "backend" / "requirements.txt").write_text("", encoding="utf-8")
    (bundle / "frontend").mkdir()
    (bundle / "frontend" / "server.js").write_text("", encoding="utf-8")
    (bundle / "node" / "bin").mkdir(parents=True)
    (bundle / ".venv" / "bin").mkdir(parents=True)

    fake_runtime = "#!/usr/bin/env bash\n[[ ${1:-} == -V ]] && echo fake-runtime\nexit 0\n"
    fake_python = bundle / ".venv" / "bin" / "python"
    fake_node = bundle / "node" / "bin" / "node"
    fake_python.write_text(fake_runtime, encoding="utf-8")
    fake_node.write_text(fake_runtime, encoding="utf-8")
    fake_python.chmod(0o755)
    fake_node.chmod(0o755)

    env = {**os.environ, "PYTHON_BIN": str(fake_python)}

    first = subprocess.run(
        ["bash", str(bundle / "install.sh")],
        cwd=bundle,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert first.returncode == 0, first.stdout + first.stderr

    generated = bundle / ".local" / "model-services.toml"
    assert generated.read_bytes() == (bundle / "model-services.example.toml").read_bytes()

    operator_config = "# operator-owned; install must preserve this\n"
    generated.write_text(operator_config, encoding="utf-8")
    second = subprocess.run(
        ["bash", str(bundle / "install.sh")],
        cwd=bundle,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert second.returncode == 0, second.stdout + second.stderr
    assert generated.read_text(encoding="utf-8") == operator_config
