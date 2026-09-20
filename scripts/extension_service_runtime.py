"""Private, content-free state and process supervision for deployment services."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

POLL_SECONDS = 0.05
CONTROL_MARGIN_SECONDS = 5.0
WORKER = Path(__file__).with_name("extension_service_worker.py")


class ServiceError(Exception):
    """Contains only a stable safe reason and, optionally, a validated service ID."""


def private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ServiceError("unsafe_state_directory")
    if not path.exists():
        private_directory(path.parent)
        path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ServiceError("unsafe_state_directory")
    # Ancestors may be an existing checkout or /tmp, so only callers' exact
    # private directory is tightened (not the directory's ancestors).


def ensure_state(path: Path) -> None:
    # Reject symlink traversal, including through an existing .local/run.
    for parent in [*reversed(path.parents), path]:
        if parent.is_symlink():
            raise ServiceError("unsafe_state_directory")
    private_directory(path)
    path.chmod(0o700)


def safe_open(path: Path, flags: int, *, allow_unlinked: bool = False) -> int:
    fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    # atomic_json can replace the path between open and fstat. A reader still
    # owns a coherent old snapshot; writers and locks must retain a linked inode.
    unlinked_snapshot = allow_unlinked and flags == os.O_RDONLY and info.st_nlink == 0
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or (info.st_nlink != 1 and not unlinked_snapshot):
        os.close(fd)
        raise ServiceError("unsafe_state_file")
    os.fchmod(fd, 0o600)
    return fd


def atomic_json(path: Path, value: object) -> None:
    if path.is_symlink():
        raise ServiceError("unsafe_state_file")
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path) -> dict:
    try:
        fd = safe_open(path, os.O_RDONLY, allow_unlinked=True)
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, encoding="utf-8") as stream:
        try:
            value = json.load(stream)
        except (ValueError, UnicodeError):
            raise ServiceError("invalid_state_file") from None
    if not isinstance(value, dict):
        raise ServiceError("invalid_state_file")
    return value


@contextmanager
def lock(path: Path, *, blocking: bool = True):
    fd = safe_open(path, os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield fd
    finally:
        os.close(fd)


def running(directory: Path, name: str = "runtime.lock") -> bool:
    try:
        with lock(directory / name, blocking=False):
            return False
    except BlockingIOError:
        return True


def event(directory: Path, run_id: str, state: str, key: str = "") -> None:
    fd = safe_open(directory / "events.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"run_id": run_id, "service": key, "state": state}) + "\n")


def stop_path(directory: Path, run_id: str) -> Path:
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ServiceError("invalid_run_identity")
    return directory / ("stop-" + run_id + ".json")


def request_stop(directory: Path, run_id: str) -> None:
    # Separate mailboxes prevent delayed cleanup of A from overwriting stop(B).
    atomic_json(stop_path(directory, run_id), {"run_id": run_id})


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def command_healthy(service: dict, timeout: float) -> bool:
    # Readiness commands get the same group guard as long-lived services, so a
    # check that exits or times out cannot leave descendants running behind it.
    with tempfile.TemporaryDirectory(prefix="silicon-extension-probe-") as temporary:
        state = Path(temporary) / "state.json"
        payload = {
            "command": service["healthcheck_command"], "cwd": service["cwd"],
            "env": service["env"], "worker_state": str(state),
            "shutdown_timeout_seconds": 0,
        }
        probe = subprocess.Popen(
            [sys.executable, str(WORKER), str(os.getpid())], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        try:
            probe.communicate(json.dumps(payload).encode(), timeout=timeout)
            return read_json(state).get("returncode") == 0
        except subprocess.TimeoutExpired:
            probe.terminate()
            try:
                probe.wait(timeout=CONTROL_MARGIN_SECONDS)
            except subprocess.TimeoutExpired:
                os.killpg(probe.pid, signal.SIGKILL)
                probe.wait()
            return False
        finally:
            if probe.stdin is not None:
                probe.stdin.close()


def http_probe(url: str, timeout: float) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(url, timeout=timeout) as response:
        return 200 <= response.status < 300


def http_healthy(url: str, timeout: float) -> bool:
    # urllib's timeout is per socket operation; a separate process enforces the
    # whole-check deadline, including DNS and slow response headers.
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_http_probe"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        process.communicate(json.dumps({"url": url, "timeout": timeout, "owner": os.getpid()}).encode(), timeout=timeout)
        return process.returncode == 0
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        return False
    finally:
        if process.stdin is not None:
            process.stdin.close()


def healthy(service: dict, timeout: float) -> bool:
    try:
        if service["healthcheck_url"]:
            return http_healthy(service["healthcheck_url"], timeout)
        return command_healthy(service, timeout)
    except (OSError, ValueError):
        return False


def shutdown(workers: list[tuple[dict, subprocess.Popen]]) -> None:
    failure = ""
    for service, process in reversed(workers):
        deadline = time.monotonic() + service["shutdown_timeout_seconds"] + CONTROL_MARGIN_SECONDS
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=max(POLL_SECONDS, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                # This Popen owns a still-live guard/group leader, never a disk PID.
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        # Even a dead guard may have a surviving witness finishing group cleanup.
        # Wait for its lease, never signal the reaped guard's numeric PID/group.
        lease = Path(service["worker_lease"])
        while running(lease.parent, lease.name):
            if time.monotonic() >= deadline:
                failure = service["key"] + ":cleanup_timeout"
                break
            time.sleep(POLL_SECONDS)
    if failure:
        raise ServiceError(failure)


def supervise(directory: Path, payload: dict) -> int:
    run_id = payload["run_id"]
    services = payload["services"]
    workers: list[tuple[dict, subprocess.Popen]] = []
    states: dict[str, dict] = {}
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def cancelled() -> bool:
        return stopping or read_json(stop_path(directory, run_id)).get("run_id") == run_id

    def publish(state: str, reason: str = "") -> None:
        atomic_json(directory / "state.json", {
            "run_id": run_id, "fingerprint": payload["fingerprint"],
            "state": state, "reason": reason, "pid": os.getpid(),
            "stop_timeout": payload["stop_timeout"], "services": states,
        })
        event(directory, run_id, state)

    def worker_live(service: dict, process: subprocess.Popen) -> bool:
        worker_state = read_json(Path(service["worker_state"])).get("state")
        return process.poll() is None and worker_state not in ("exited", "failed")

    with lock(directory / "runtime.lock", blocking=False) as runtime_fd, lock(directory / "supervisor.lock", blocking=False):
        failure = ""
        publish("starting")
        try:
            for service in services:
                key = service["key"]
                if cancelled():
                    raise ServiceError("startup_cancelled")
                process = None
                if service["mode"] == "managed":
                    service["worker_state"] = str(directory / (run_id + "-" + str(len(workers)) + ".json"))
                    service["worker_lease"] = str(Path(service["worker_state"]).with_suffix(".lock"))
                    with lock(Path(service["worker_lease"]), blocking=False) as group_fd:
                        process = subprocess.Popen(
                            [sys.executable, str(WORKER), str(os.getpid())], stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                            pass_fds=(runtime_fd, group_fd),
                        )
                    workers.append((service, process))
                    process.stdin.write(json.dumps(service).encode())
                    process.stdin.close()
                states[key] = {"state": "starting", "pid": process.pid if process else None}
                publish("starting")
                deadline = time.monotonic() + service["startup_timeout_seconds"]
                while True:
                    if cancelled():
                        raise ServiceError("startup_cancelled")
                    for owned_service, owned in workers:
                        if not worker_live(owned_service, owned):
                            raise ServiceError(owned_service["key"] + ":process_exited")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ServiceError(key + ":readiness_timeout")
                    if healthy(service, min(remaining, service["healthcheck_timeout_seconds"])):
                        if cancelled():
                            raise ServiceError("startup_cancelled")
                        if time.monotonic() >= deadline:
                            raise ServiceError(key + ":readiness_timeout")
                        # Check the guard's child state after a successful probe.
                        if process is None or (read_json(Path(service["worker_state"])).get("state") == "running" and worker_live(service, process)):
                            break
                    time.sleep(POLL_SECONDS)
                states[key]["state"] = "ready"
                event(directory, run_id, "ready", key)
            publish("ready")
            while not cancelled():
                for service, process in workers:
                    if not worker_live(service, process):
                        raise ServiceError(service["key"] + ":process_exited")
                time.sleep(POLL_SECONDS)
        except ServiceError as exc:
            failure = str(exc)
        except Exception:
            failure = "service_runtime_failed"
        finally:
            publish("stopping", failure)
            try:
                shutdown(workers)
            except ServiceError as exc:
                failure = str(exc)
            for value in states.values():
                value["state"] = "stopped"
                value["pid"] = None
            publish("failed" if failure else "stopped", failure)
            for service, _ in workers:
                lease = Path(service["worker_lease"])
                if not running(lease.parent, lease.name):
                    Path(service["worker_state"]).unlink(missing_ok=True)
                    lease.unlink(missing_ok=True)
    return 1 if failure else 0


def guarded_http_probe(request: dict) -> bool:
    # A daemon thread cannot retain this disposable interpreter. The foreground
    # owner watcher also exits if the supervisor dies during blocked DNS/I/O.
    result = [False]
    def probe() -> None:
        try:
            result[0] = http_probe(request["url"], request["timeout"])
        except Exception:
            pass
    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    deadline = time.monotonic() + request["timeout"]
    while thread.is_alive():
        if os.getppid() != request["owner"] or time.monotonic() >= deadline:
            return False
        thread.join(POLL_SECONDS)
    return result[0]


if __name__ == "__main__":
    try:
        request = json.load(sys.stdin)
        raise SystemExit(0 if guarded_http_probe(request) else 1)
    except Exception:
        raise SystemExit(1) from None
