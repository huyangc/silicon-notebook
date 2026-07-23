from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess


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
