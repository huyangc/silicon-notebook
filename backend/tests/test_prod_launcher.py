from __future__ import annotations

import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import json
import sys
import tempfile
import time

import pytest


pytestmark = pytest.mark.xdist_group("prod_script_lifecycle")

_GIT_ISOLATION = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    # A checkout under the temp dir must not discover an enclosing repository.
    "GIT_CEILING_DIRECTORIES": str(Path(tempfile.gettempdir()).resolve()),
}


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _prepare_launcher(tmp_path: Path) -> tuple[Path, Path, Path, Path, dict[str, str]]:
    repository_root = Path(__file__).resolve().parents[2]
    root = tmp_path / "checkout"
    scripts = root / "scripts"
    backend = root / "backend"
    fake_bin = root / "fake-bin"
    next_bin = root / "frontend" / "node_modules" / ".bin"
    for directory in (scripts, backend, fake_bin, next_bin):
        directory.mkdir(parents=True, exist_ok=True)

    (scripts / "prod.sh").write_text(
        (repository_root / "scripts" / "prod.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (scripts / "autotune.sh").write_text("# test stub\n", encoding="utf-8")
    for name in ("python_env.py", "extension_services.sh", "extension_services.py", "extension_service_runtime.py", "extension_service_worker.py", "build_release_manifest.py"):
        shutil.copy2(repository_root / "scripts" / name, scripts / name)
    (root / ".env").write_text("# test env\n", encoding="utf-8")
    (backend / "requirements.txt").write_text("# test requirements\n", encoding="utf-8")

    _write_executable(
        fake_bin / "python",
        """#!/usr/bin/env bash
if [[ "${1:-}" == "-m" && "${2:-}" == "pip" ]]; then
  printf 'python %s\n' "$*" >>"$INSTALL_CALLS_FILE"
  exit 0
fi
if [[ "${1:-}" == "-c" || "${1:-}" == */extension_services.py || "${1:-}" == */build_release_manifest.py ]]; then
  exec "$REAL_PYTHON" "$@"
fi
exec sleep 30
""",
    )
    _write_executable(
        fake_bin / "npm",
        """#!/usr/bin/env bash
printf 'npm %s\n' "$*" >>"$INSTALL_CALLS_FILE"
""",
    )
    _write_executable(
        next_bin / "next",
        """#!/usr/bin/env bash
exec sleep 30
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
url=""
for arg in "$@"; do
  case "$arg" in http://*) url="$arg" ;; esac
done
printf '%s\n' "$url" >>"$CURL_CALLS_FILE"
if [[ "$url" == */api/ready ]]; then
  printf '{"ready":true}\n'
fi
""",
    )
    _write_executable(fake_bin / "ss", "#!/usr/bin/env bash\nexit 0\n")

    calls_file = root / "curl-calls.txt"
    install_calls_file = root / "install-calls.txt"
    env = {
        # Inherited GIT_* (hooks, `rebase -x`) would point git at the real repo.
        **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        **_GIT_ISOLATION,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "PYTHON_BIN": str(fake_bin / "python"),
        "REAL_PYTHON": sys.executable,
        "CURL_CALLS_FILE": str(calls_file),
        "INSTALL_CALLS_FILE": str(install_calls_file),
        "SKIP_BUILD": "1",
        "PORT": "18800",
        "FRONTEND_PORT": "18801",
    }
    return root, scripts, fake_bin, next_bin, env


def test_prod_launcher_detaches_without_waiting_for_readiness(tmp_path: Path) -> None:
    root, scripts, _fake_bin, _next_bin, env = _prepare_launcher(tmp_path)
    calls_file = Path(env["CURL_CALLS_FILE"])
    install_calls_file = Path(env["INSTALL_CALLS_FILE"])
    backend = root / "backend"
    launched_pids: list[int] = []
    try:
        completed = subprocess.run(
            ["bash", str(scripts / "prod.sh")],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "background processes were launched" in completed.stdout
        assert "readiness is not checked" in completed.stdout
        assert "stop them with: npm run stop" in completed.stdout

        launched_pids = [int(value) for value in re.findall(r"\(PID (\d+),", completed.stdout)]
        assert len(launched_pids) == 2
        assert all(_pid_is_alive(pid) for pid in launched_pids)

        install_calls = install_calls_file.read_text(encoding="utf-8").splitlines()
        assert install_calls == [
            f"python -m pip install --disable-pip-version-check -r {backend / 'requirements.txt'}",
            f"npm ci --prefix {root / 'frontend'}",
        ]

        assert not calls_file.exists()
    finally:
        for pid in launched_pids:
            if _pid_is_alive(pid):
                os.kill(pid, signal.SIGTERM)
        time.sleep(0.05)


def test_port_preflight_detects_ss_listener_without_visible_pid(tmp_path: Path) -> None:
    root, scripts, fake_bin, _next_bin, env = _prepare_launcher(tmp_path)
    _write_executable(
        fake_bin / "ss",
        """#!/usr/bin/env bash
printf 'LISTEN 0 4096 0.0.0.0:18800 0.0.0.0:*\n'
""",
    )

    completed = subprocess.run(
        ["bash", str(scripts / "prod.sh")],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "backend 端口 :18800 已被占用(PID unavailable)" in completed.stderr
    assert not Path(env["INSTALL_CALLS_FILE"]).exists()


def _git(root: Path, env: dict[str, str], *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "commit.gpgsign=false", *args],
        env=env, text=True, capture_output=True, check=True,
    ).stdout.strip()


def _commit_checkout(root: Path, env: dict[str, str], notes: dict[str, str]) -> None:
    """Turn the prepared checkout into a git repository with the given release notes."""
    for name, body in notes.items():
        path = root / "release-notes" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    _git(root, env, "init", "-q")
    _git(root, env, "add", "-A")
    _git(root, env, "commit", "-q", "-m", "checkout")


def _launch(scripts: Path, root: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run prod.sh to completion and stop the two detached services it leaves behind."""
    completed = subprocess.run(
        ["bash", str(scripts / "prod.sh")],
        cwd=root, env=env, text=True, capture_output=True, timeout=20, check=False,
    )
    for pid in (int(value) for value in re.findall(r"\(PID (\d+),", completed.stdout)):
        if _pid_is_alive(pid):
            os.kill(pid, signal.SIGTERM)
    return completed


def test_prod_launcher_generates_the_release_manifest_from_the_checkout(tmp_path: Path) -> None:
    root, scripts, _fake_bin, _next_bin, env = _prepare_launcher(tmp_path)
    _commit_checkout(root, env, {"report-export.md": (
        "---\nlevel: feature\naudience: all\ntitle: 报告可以导出为 Word 文件\n---\n"
        "报告现在可以导出为 Word 文件。\n"
    )})

    completed = _launch(scripts, root, env)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    manifest = json.loads((root / "release-manifest.json").read_text(encoding="utf-8"))
    head = _git(root, env, "rev-parse", "HEAD")
    assert manifest["build"]["sha"] == head
    assert manifest["build"]["ordinal"] == 1
    assert re.fullmatch(rf"\d{{8}}-{head[:7]}", manifest["build"]["version"])
    assert manifest["schema"] == 2
    assert [
        (n["id"], n["ordinal"], n["level"], n["audience"], n["title"], n["body"])
        for n in manifest["notes"]
    ] == [
        ("report-export", 1, "feature", "all", "报告可以导出为 Word 文件",
         "报告现在可以导出为 Word 文件。")
    ]
    assert f"release manifest: {root / 'release-manifest.json'}" in completed.stdout


def test_prod_launcher_still_starts_and_drops_a_stale_manifest_when_generation_fails(
    tmp_path: Path,
) -> None:
    root, scripts, _fake_bin, _next_bin, env = _prepare_launcher(tmp_path)
    _commit_checkout(root, env, {"empty.md": "   \n"})
    stale = root / "release-manifest.json"
    stale.write_text('{"stale": true}\n', encoding="utf-8")

    completed = _launch(scripts, root, env)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "background processes were launched" in completed.stdout
    assert "empty.md" in completed.stderr
    assert "生成 release-manifest.json 失败" in completed.stderr
    assert not stale.exists()


def test_prod_launcher_still_starts_and_drops_a_stale_manifest_outside_a_git_checkout(
    tmp_path: Path,
) -> None:
    root, scripts, _fake_bin, _next_bin, env = _prepare_launcher(tmp_path)
    stale = root / "release-manifest.json"
    stale.write_text('{"stale": true}\n', encoding="utf-8")

    completed = _launch(scripts, root, env)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "background processes were launched" in completed.stdout
    assert "不是 git 仓库根目录" in completed.stderr
    assert not stale.exists()
