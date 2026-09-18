#!/usr/bin/env python3
"""Manage deployment extension services without loading application bundles."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import time

from extension_service_runtime import (
    CONTROL_MARGIN_SECONDS, POLL_SECONDS, ServiceError, atomic_json, ensure_state,
    healthy, lock, read_json, request_stop, running, safe_open, stop_path, supervise,
)

ROOT = Path(__file__).resolve().parent.parent


def configuration(root: Path) -> tuple[list[dict], str]:
    from python_env import build_python_environment

    environment = build_python_environment(root=root)
    config = environment.get("EXTENSIONS_CONFIG", "").strip()
    if not config:
        return [], ""
    # No application/bootstrap imports: the parser is deployment-only.
    sys.path.insert(0, str(root / "backend"))
    from app.extensions.service_config import ServiceConfigError, parse_service_config

    config_path = Path(config)
    if not config_path.is_absolute():
        config_path = root / config_path
    try:
        specs = parse_service_config(str(config_path))
    except ServiceConfigError as exc:
        raise ServiceError(str(exc)) from None
    services = []
    for spec in specs:
        service = asdict(spec)
        service["key"] = spec.key
        service["cwd"] = str(spec.cwd.resolve())
        if not spec.cwd.is_dir():
            raise ServiceError(spec.key + ":working_directory_missing")
        resolved_environment = {**environment, **spec.env}
        for name, source in spec.env_from.items():
            if source not in environment:
                raise ServiceError(spec.key + ":environment_reference_missing")
            resolved_environment[name] = environment[source]
        service["env"] = resolved_environment
        for command in (spec.command, spec.healthcheck_command):
            if not command:
                continue
            executable = command[0]
            if os.sep in executable:
                path = Path(executable)
                if not path.is_absolute():
                    path = spec.cwd / path
                valid = path.is_file() and os.access(path, os.X_OK)
            else:
                search_path = os.pathsep.join(
                    str(spec.cwd / entry) if not Path(entry).is_absolute() else entry
                    for entry in resolved_environment.get("PATH", os.defpath).split(os.pathsep)
                )
                valid = shutil.which(executable, path=search_path) is not None
            if not valid:
                raise ServiceError(spec.key + ":command_unavailable")
        services.append(service)
    if services:
        checked = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_cli_extensions.py")],
            cwd=root, env=environment, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if checked.returncode:
            raise ServiceError("plugin_discovery_failed")
    # Shell bookkeeping changes between CLI and npm entry points and has no
    # plugin configuration meaning; all other effective values remain pinned.
    fingerprint_services = [
        {**service, "env": {key: value for key, value in service["env"].items()
                           if key not in {"_", "SHLVL", "PWD", "OLDPWD"}}}
        for service in services
    ]
    fingerprint = hashlib.sha256(json.dumps(fingerprint_services, sort_keys=True).encode()).hexdigest()
    return services, fingerprint


def write_receipt(path: Path | None, run_id: str, owned: bool) -> None:
    if path is not None:
        atomic_json(path, {"run_id": run_id, "owned": owned})


def await_stop(directory: Path, timeout: float, run_id: str | None = None) -> None:
    deadline = time.monotonic() + timeout
    while running(directory):
        if run_id is not None and read_json(directory / "state.json").get("run_id") != run_id:
            return
        if time.monotonic() >= deadline:
            raise ServiceError("stop_timeout")
        time.sleep(POLL_SECONDS)


def _spawn(directory: Path, services: list[dict], fingerprint: str, receipt: Path | None, owner: dict):
    state = read_json(directory / "state.json")
    if running(directory):
        if not running(directory, "supervisor.lock") or state.get("state") != "ready" or state.get("fingerprint") != fingerprint:
            raise ServiceError("running_configuration_changed_stop_before_start")
        write_receipt(receipt, state["run_id"], False)
        return {"state": "rechecking", "run_id": state["run_id"]}
    if not services:
        write_receipt(receipt, "", False)
        return {"state": "disabled"}
    run_id = secrets.token_hex(16)
    stop_timeout = (
        sum(s["shutdown_timeout_seconds"] + CONTROL_MARGIN_SECONDS for s in services if s["mode"] == "managed")
        + max(s["healthcheck_timeout_seconds"] for s in services) + CONTROL_MARGIN_SECONDS
    )
    payload = {"run_id": run_id, "services": services, "fingerprint": fingerprint, "stop_timeout": stop_timeout}
    owner.update(run_id=run_id, stop_timeout=stop_timeout)
    # Receipt precedes spawn so an interrupted launcher retains ownership.
    write_receipt(receipt, run_id, True)
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_supervise", "--state-dir", str(directory)],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    owner["process"] = process
    process.stdin.write(json.dumps(payload).encode())
    process.stdin.close()
    deadline = time.monotonic() + CONTROL_MARGIN_SECONDS
    while True:
        state = read_json(directory / "state.json")
        if state.get("run_id") == run_id:
            if state.get("state") in ("failed", "stopped"):
                raise ServiceError(state.get("reason") or "startup_failed")
            if running(directory):
                break
        if process.poll() is not None or time.monotonic() >= deadline:
            raise ServiceError("supervisor_start_failed")
        time.sleep(POLL_SECONDS)
    return process, run_id


def rollback(directory: Path, owner: dict) -> None:
    run_id = owner.get("run_id")
    if run_id is None:
        return
    request_stop(directory, run_id)
    process = owner.get("process")
    if process is None:
        # If cancellation interrupted Popen construction, a subsequently running
        # daemon sees its pre-existing cancellation before launching children.
        return
    if process.stdin is not None:
        try:
            process.stdin.close()
        except OSError:
            pass
    timeout = owner["stop_timeout"] + CONTROL_MARGIN_SECONDS
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.terminate()
        process.wait(timeout=timeout)
    stop_path(directory, run_id).unlink(missing_ok=True)


def recheck_reused(directory: Path, services: list[dict], fingerprint: str, run_id: str) -> dict:
    def require_same_ready_session() -> None:
        state = read_json(directory / "state.json")
        if (state.get("run_id") != run_id or state.get("state") != "ready"
            or state.get("fingerprint") != fingerprint
            or not running(directory) or not running(directory, "supervisor.lock")
            or read_json(stop_path(directory, run_id)).get("run_id") == run_id):
            raise ServiceError("reused_session_changed")

    # A ready snapshot is not a current health guarantee, especially for external
    # services. Do not hold operations.lock during probes or claim this session:
    # another terminal must remain able to stop it, and failure must leave it alone.
    for service in services:
        deadline = time.monotonic() + service["startup_timeout_seconds"]
        while True:
            require_same_ready_session()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ServiceError(service["key"] + ":readiness_timeout")
            ready = healthy(service, min(remaining, service["healthcheck_timeout_seconds"]))
            require_same_ready_session()
            if ready and time.monotonic() < deadline:
                break
            time.sleep(POLL_SECONDS)
    with lock(directory / "operations.lock"):
        require_same_ready_session()
        return {"state": "ready", "reused": True}


def start(directory: Path, services: list[dict], fingerprint: str, receipt: Path | None) -> dict:
    # Hold the control lock only through the daemon ownership handshake; status
    # and a second terminal's stop remain available throughout readiness waits.
    owner: dict = {}
    try:
        with lock(directory / "operations.lock"):
            spawned = _spawn(directory, services, fingerprint, receipt, owner)
        if isinstance(spawned, dict):
            if spawned["state"] == "rechecking":
                return recheck_reused(directory, services, fingerprint, spawned["run_id"])
            return spawned
        process, run_id = spawned
        deadline = time.monotonic() + sum(s["startup_timeout_seconds"] for s in services) + CONTROL_MARGIN_SECONDS
        while True:
            state = read_json(directory / "state.json")
            if state.get("run_id") == run_id:
                if state.get("state") == "ready":
                    return {"state": "ready", "reused": False}
                if state.get("state") in ("stopping", "failed", "stopped"):
                    raise ServiceError(state.get("reason") or "startup_failed")
            if process.poll() is not None:
                raise ServiceError("supervisor_start_failed")
            if time.monotonic() >= deadline:
                raise ServiceError("startup_timeout")
            time.sleep(POLL_SECONDS)
    except BaseException:
        rollback(directory, owner)
        raise


def stop(directory: Path, receipt: Path | None) -> dict:
    with lock(directory / "operations.lock"):
        state = read_json(directory / "state.json")
        if receipt is not None:
            ownership = read_json(receipt)
            if ownership.get("owned") is not True or ownership.get("run_id") != state.get("run_id"):
                return {"state": "not_owned"}
        if not running(directory):
            return {"state": "stopped"}
        request_stop(directory, state.get("run_id"))
    await_stop(directory, state.get("stop_timeout", CONTROL_MARGIN_SECONDS), state.get("run_id"))
    stop_path(directory, state["run_id"]).unlink(missing_ok=True)
    return {"state": "stopped"}


def status(directory: Path) -> dict:
    state = read_json(directory / "state.json")
    live = running(directory)
    supervisor_live = live and running(directory, "supervisor.lock")
    if not live:
        # A graceful shutdown publishes its terminal state before unlocking;
        # refresh after checking the lease to avoid reporting that race as loss.
        state = read_json(directory / "state.json")
    interrupted = not supervisor_live and state.get("state") in ("starting", "ready", "stopping")
    if supervisor_live:
        current_state = state.get("state", "starting")
    elif live:
        current_state = "stopping"
    else:
        current_state = "failed" if interrupted or state.get("state") == "failed" else "stopped"
    return {
        "state": current_state,
        "reason": "supervisor_exited" if interrupted else state.get("reason", ""),
        "pid": state.get("pid") if supervisor_live else None,
        "services": state.get("services", {}) if live else {
            key: {"state": "stopped", "pid": None} for key in state.get("services", {})
        },
    }


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="插件配套服务统一启停、状态与无内容生命周期日志。")
    parser.add_argument("action", choices=("start", "stop", "status", "logs", "validate", "_supervise"), metavar="{start,stop,status,logs,validate}")
    parser.add_argument("--receipt", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--state-dir", type=Path, default=ROOT / ".local" / "run" / "extensions", help=argparse.SUPPRESS)
    args = parser.parse_args(arguments)
    if args.action != "_supervise":
        def interrupted(_signum: int, _frame: object) -> None:
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGHUP, interrupted)
        signal.signal(signal.SIGINT, interrupted)
    try:
        directory = args.state_dir.absolute()
        ensure_state(directory)
        if args.action == "_supervise":
            return supervise(directory, json.load(sys.stdin))
        if args.action in ("start", "validate"):
            services, fingerprint = configuration(ROOT)
            result = start(directory, services, fingerprint, args.receipt) if args.action == "start" else {"state": "valid", "services": [s["key"] for s in services]}
        elif args.action == "stop":
            result = stop(directory, args.receipt)
        elif args.action == "status":
            result = status(directory)
        else:
            try:
                fd = safe_open(directory / "events.jsonl", os.O_RDONLY)
            except FileNotFoundError:
                return 0
            with os.fdopen(fd, encoding="utf-8") as stream:
                for line in stream:
                    print(line, end="")
            return 0
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except KeyboardInterrupt:
        print("插件服务操作已取消，请运行状态命令确认当前服务状态。", file=sys.stderr)
        return 130
    except ServiceError as exc:
        print("插件服务操作失败：" + str(exc) + "；请检查插件服务配置或先停止现有服务。", file=sys.stderr)
        return 2
    except Exception:
        print("插件服务操作失败，请检查配置、运行目录权限和解释器环境。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
