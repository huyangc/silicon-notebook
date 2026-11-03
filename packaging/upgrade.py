"""Stopped, offline deployment upgrade without repository access or migration."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import uuid
from pathlib import Path

# Only release-owned paths are replaced; all other deployment files stay put.
PAYLOAD = (
    "backend/app", "backend/requirements.txt", "frontend", "node", "scripts",
    "wheelhouse", ".env.example", "model-services.example.toml", "install.sh",
    "start.sh", "stop.sh", "upgrade.sh", "upgrade.py", "DEPLOY.md", "VERSION",
    "release-manifest.json",
)
OPTIONAL = {"release-manifest.json"}


class UpgradeError(Exception):
    pass


def sync_parents(path: Path, root: Path) -> None:
    while path.is_relative_to(root):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        if path == root:
            break
        path = path.parent


def run_command(args: list[str], *, cwd: Path, env: dict | None = None, capture: bool = False):
    """Cancel and reap the installation process tree before touching its files."""
    with subprocess.Popen(args, cwd=cwd, env=env, start_new_session=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None, text=True) as process:
        try:
            stdout, _ = process.communicate()
            if process.returncode:
                raise UpgradeError("环境准备或配置检查失败；请检查离线依赖与原配置，旧安装保持不变。")
            return stdout
        except BaseException:
            # Kill the whole private group, including pip/build descendants.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise


def inspect_existing(target: Path) -> dict:
    script = r'''
import importlib.metadata as metadata, json, os, pathlib, sys
root = pathlib.Path(sys.argv[1])
sys.path.insert(0, str(root / "backend"))
from app.core.config import Settings
s = Settings()
paths = [s.storage_dir, s.model_services_config, s.extensions_config]
paths.append(os.environ.get("SILICON_NOTEBOOK_ENV_FILE", ""))
paths.append(_upgrade_selected_env_file)
if s.database_url.startswith("sqlite:///"):
    paths.append(s.sqlite_path)
for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep):
    if entry and pathlib.Path(os.path.abspath(root / entry)) != root / "backend":
        paths.append(entry)
packages = [(d.metadata["Name"], d.version) for d in metadata.distributions()]
print(json.dumps({"paths": [p for p in paths if p], "packages": packages}))
'''
    try:
        output = run_config_python(target / ".venv/bin/python", script, target)
        return json.loads(output)
    except (OSError, ValueError, UpgradeError):
        raise UpgradeError("无法检查原环境及配置；请先确认原 .venv 可用、原配置可读取，再重试升级。") from None


def run_config_python(python: Path, script: str, target: Path, *args: str, env: dict | None = None):
    # Match both start.sh's shell expansion and python_env.py's selected-file
    # precedence/path preparation. Never replace an explicit selection (or its
    # empty opt-out) with the root .env while inspecting or checking new code.
    release_root = Path(__file__).resolve().parent
    helper = release_root / "scripts/python_env.py"
    if not helper.is_file():
        helper = release_root.parent / "scripts/python_env.py"  # source checkout
    bootstrap = r'''
import os, pathlib, runpy, sys
helper, script = sys.argv[1:3]
root = pathlib.Path(sys.argv[3])
selected = os.environ.get("SILICON_NOTEBOOK_ENV_FILE", "")
prepared = runpy.run_path(helper)["build_python_environment"](root=root)
prepared.setdefault("SILICON_NOTEBOOK_ENV_FILE", str(root / ".env"))
os.environ.clear()
os.environ.update(prepared)
sys.path[:0] = [p for p in prepared["PYTHONPATH"].split(os.pathsep) if p]
sys.argv = ["-c", *sys.argv[3:]]
exec(compile(script, "<upgrade-config>", "exec"), {
    "__name__": "__main__", "_upgrade_selected_env_file": selected.strip(),
})
'''
    shell = 'set -euo pipefail; set -a; source "$1/.env"; set +a; exec "$2" -c "$3" "${@:4}"'
    return run_command(["bash", "-c", shell, "upgrade-config", str(target), str(python), bootstrap, str(helper), script, str(target), *args],
                       cwd=target, env=env, capture=True)


def core_requirements(root: Path) -> dict[str, str]:
    def normalize(name):
        return re.sub(r"[-_.]+", "-", name).lower()
    text = (root / "backend/requirements.txt").read_text(encoding="utf-8")
    return {normalize(name): version for name, version in re.findall(r"^([\w.-]+)==([^\s\\]+)", text, re.M)}


def remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def save_journal(work: Path, journal: list[dict]) -> None:
    temporary = work / "transaction.tmp"
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(journal, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(work / "transaction.json")
    sync_parents(work, work.parent)


def rollback(target: Path, work: Path) -> None:
    journal = json.loads((work / "transaction.json").read_text(encoding="utf-8"))
    for item in reversed(journal):
        name = item["name"]
        if name not in (*PAYLOAD, ".venv"):
            raise UpgradeError("升级恢复记录无效，请保留现场并联系维护人员。")
        live, old = target / name, work / "previous" / name
        if old.exists() or old.is_symlink():
            remove(live)
            old.replace(live)
            sync_parents(live.parent, target)
            sync_parents(old.parent, target)
        elif not item["existed"]:
            remove(live)
    remove(work)


def assert_stopped(target: Path) -> None:
    run = target / ".local/run"
    pidfiles = [run / f"{name}.pid" for name in ("backend", "frontend")]
    pidfiles.extend(run.glob("package-start.*.pid"))
    for pidfile in pidfiles:
        if not pidfile.exists():
            continue
        try:
            pid = int(pidfile.read_text(encoding="utf-8").strip())
            if pid <= 0:
                raise ValueError
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except (ValueError, PermissionError):
            raise UpgradeError("无法确认服务已停止；请在原目录执行 ./stop.sh 并检查进程。") from None
        raise UpgradeError("服务仍在运行；请先在原目录执行 ./stop.sh。")


def validate(source: Path, target: Path) -> list[str]:
    if source == target or source.is_relative_to(target) or target.is_relative_to(source):
        raise UpgradeError("请将新包解压到原安装目录之外，再指定原目录为 --target。")
    if not (target / ".env").is_file() or not (target / "start.sh").is_file():
        raise UpgradeError("目标不是已配置的安装目录；请核对 --target，首次安装请用 ./install.sh。")
    for name in PAYLOAD:
        if name not in OPTIONAL and not (source / name).exists():
            raise UpgradeError("新包不完整；请重新打包并完整解压。")
        # Never follow deployment symlinks into data or another installation.
        for path in (target / name, (target / name).parent):
            if path.is_symlink():
                raise UpgradeError("程序目录包含符号链接；请先恢复普通程序目录再升级。")
    version = dict(line.split("=", 1) for line in (source / "VERSION").read_text().splitlines() if "=" in line)
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    if version.get("platform") != f"{platform.system().lower()}-{arch}":
        raise UpgradeError("新包系统或架构与本机不匹配；请在同系统、同架构的打包机重新打包。")
    assert_stopped(target)
    existing = inspect_existing(target)
    for value in [str(target / ".env"), *existing["paths"]]:
        path = Path(value)
        logical = Path(os.path.abspath(target / path))
        for candidate in (logical, logical.resolve()):
            for name in (*PAYLOAD, ".venv"):
                owned = target / name
                if candidate == owned or candidate.is_relative_to(owned) or owned.is_relative_to(candidate):
                    raise UpgradeError("数据、配置或插件路径与发行程序文件重叠；请先迁至 .local 或安装目录外并更新原配置，再升级。")
    core = set(core_requirements(source)) | {"pip", "setuptools", "wheel"}
    extras = []
    for name, version in existing["packages"]:
        if not name or not re.fullmatch(r"[\w.-]+", name) or not re.fullmatch(r"[\w.!+-]+", version):
            raise UpgradeError("原环境含无法离线重装的包；请先整理环境并提供对应 wheel。")
        if re.sub(r"[-_.]+", "-", name).lower() not in core:
            extras.append(f"{name}=={version}")
    return extras


def upgrade(source: Path, target: Path, work: Path, extra_wheelhouse: tuple[Path, ...] = ()) -> None:
    extras = validate(source, target)
    if extras and not extra_wheelhouse:
        raise UpgradeError("原环境有额外安装的包或插件；请准备其完整离线 wheel 及依赖，并加 --extra-wheelhouse /离线依赖目录 后重试。")
    if any(not path.is_dir() for path in extra_wheelhouse):
        raise UpgradeError("额外离线依赖目录不存在，请核对 --extra-wheelhouse。")
    work.mkdir(mode=0o700)
    journal: list[dict] = []
    runtime = target / ".local/upgrade-venvs" / uuid.uuid4().hex
    try:
        save_journal(work, journal)
        stage = work / "next"
        stage.mkdir()
        for name in PAYLOAD:
            src, dst = source / name, stage / name
            if not src.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst)
        print("[upgrade] 正在离线准备独立运行环境；原配置与数据保留。", flush=True)
        env = {**os.environ, "PYTHON_BIN": sys.executable, "VENV_DIR": str(runtime), "OFFLINE": "1"}
        # No inherited shell hooks or pip target/prefix may redirect the installer.
        for key in ("BASH_ENV", "ENV", "PIP_TARGET", "PIP_PREFIX", "PIP_USER"):
            env.pop(key, None)
        run_command(["bash", str(stage / "install.sh")], cwd=stage, env=env)
        if extras:
            # Explicit local wheels only; constrain shared deps to the core lock.
            requirements = stage / "extra-requirements.txt"
            requirements.write_text("\n".join(extras) + "\n", encoding="utf-8")
            constraints = stage / "extra-constraints.txt"
            constraints.write_text("\n".join(f"{name}=={version}" for name, version in core_requirements(source).items()) + "\n", encoding="utf-8")
            pip_env = {k: v for k, v in env.items() if not k.startswith("PIP_")}
            pip_env.update(PIP_NO_INDEX="1", PIP_CONFIG_FILE="/dev/null", PIP_DISABLE_PIP_VERSION_CHECK="1")
            args = [str(runtime / "bin/python"), "-m", "pip", "install", "--no-index", "--no-deps", "--only-binary=:all:", "--find-links", str(stage / "wheelhouse")]
            for path in extra_wheelhouse:
                args.extend(["--find-links", str(path)])
            args.extend(["-c", str(constraints), "-r", str(requirements)])
            run_command(args, cwd=stage, env=pip_env, capture=True)
            run_command([str(runtime / "bin/python"), "-m", "pip", "check"], cwd=stage, env=pip_env, capture=True)
        # Validate the original configuration against new code without opening a
        # repository or running migrations. Relative deployment paths stay rooted
        # in target, while imports deliberately select the staged backend.
        check_script = r'''
import os, pathlib, sys
target, stage = map(pathlib.Path, sys.argv[1:])
sys.path.insert(0, str(stage / "backend"))
from app.core import config
config._ROOT_DIR = target
from app.extensions.discovery import discover_deployment_extensions
from app.services.model_registry import SystemModelServiceRegistry
for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep):
    if entry:
        path = pathlib.Path(entry)
        sys.path.append(str(path if path.is_absolute() else target / path))
s = config.Settings()
SystemModelServiceRegistry.load(s)
discover_deployment_extensions(s.extensions_config)
'''
        run_config_python(runtime / "bin/python", check_script, target, str(stage), env=env)
        assert_stopped(target)
        (stage / ".venv").symlink_to(runtime, target_is_directory=True)
        for name in (*PAYLOAD, ".venv"):
            live, incoming, old = target / name, stage / name, work / "previous" / name
            old.parent.mkdir(parents=True, exist_ok=True)
            journal.append({"name": name, "existed": live.exists() or live.is_symlink()})
            save_journal(work, journal)  # write-ahead: recovery also handles mid-rename interruption
            if live.exists() or live.is_symlink():
                live.replace(old)
                sync_parents(old.parent, target)
                sync_parents(live.parent, target)
            if incoming.exists() or incoming.is_symlink():
                incoming.replace(live)
                sync_parents(live.parent, target)
        # Keeping previous code/env does not roll back a later database migration.
        archive = target / ".local/upgrade-backups" / uuid.uuid4().hex
        archive.parent.mkdir(parents=True, exist_ok=True)
        work.replace(archive)
        sync_parents(archive.parent, target)
    except BaseException:
        if work.exists():
            if (work / "transaction.json").exists():
                rollback(target, work)
            else:
                remove(work)  # no journal was published, so no replacement began
            remove(runtime)
        raise
    print("[upgrade] 原地升级完成，配置与数据路径保持不变。请在原目录执行 ./start.sh，再检查 /api/ready。")
    print("[upgrade] 上一版程序与环境已保存在 .local/upgrade-backups；首次启动后的数据回退需要升级前备份。")


def main() -> int:
    parser = argparse.ArgumentParser(description="离线原地升级（先停服并备份数据库和存储）。")
    parser.add_argument("--target", required=True, type=Path, help="原安装目录")
    parser.add_argument("--recover", action="store_true", help="恢复中断升级前的程序与运行环境，不操作数据")
    parser.add_argument("--extra-wheelhouse", action="append", type=Path, default=[], help="原环境额外包及插件的离线 wheel 目录，可重复")
    args = parser.parse_args()
    if sys.version_info < (3, 13):
        parser.error("需要 Python >= 3.13；请设置 PYTHON_BIN 后重试。")
    target = args.target.resolve()
    if not target.is_dir():
        parser.error("原安装目录不存在，请核对 --target。")
    local = target / ".local"
    local.mkdir(exist_ok=True)
    work = local / "offline-upgrade"
    try:
        with (local / "offline-upgrade.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise UpgradeError("另一个升级正在运行，请等待完成。") from None
            assert_stopped(target)
            if args.recover:
                if not work.is_dir():
                    raise UpgradeError("没有待恢复的升级。")
                if (work / "transaction.json").is_file():
                    rollback(target, work)
                elif not (work / "previous").exists():
                    remove(work)
                else:
                    raise UpgradeError("恢复记录缺失，请保留现场并联系维护人员。")
                print("[upgrade] 已恢复升级前的程序与运行环境，配置和数据保持原样。")
            elif work.exists():
                raise UpgradeError("上次升级中断；请先用相同 --target 加 --recover 恢复，再重试升级。")
            else:
                upgrade(Path(__file__).resolve().parent, target, work, tuple(path.resolve() for path in args.extra_wheelhouse))
        return 0
    except KeyboardInterrupt:
        print("[upgrade] 已取消；请保持停服。若存在中断记录，请加 --recover 恢复后再操作。", file=sys.stderr)
        return 130
    except (UpgradeError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        # Exception text can contain paths, credentials or deployment content.
        print("[upgrade:错误] " + (str(exc) if isinstance(exc, UpgradeError) else "升级未完成；请检查离线包、磁盘空间和权限。若提示存在中断升级，请先加 --recover 恢复。"), file=sys.stderr)
        return 1


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise KeyboardInterrupt

    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupted)
    raise SystemExit(main())
