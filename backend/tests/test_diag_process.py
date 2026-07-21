from __future__ import annotations

import ast
import importlib.util
import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "diag_process.py"


def load_process_module():
    spec = importlib.util.spec_from_file_location("diag_process", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_script_is_standalone_stdlib_only_and_help_runs_without_backend_path():
    tree = ast.parse(SCRIPT.read_text())
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
    assert imported_roots <= sys.stdlib_module_names | {"__future__"}
    assert "app" not in imported_roots
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=REPO,
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    assert result.returncode == 0
    assert "--diagnostics-dir" in result.stdout
    assert result.stderr == ""


def proc_stat(
    pid: int,
    *,
    name: str = "uvicorn worker (main)",
    state: str = "S",
    user_ticks: int = 10,
    system_ticks: int = 5,
    start_ticks: int = 100,
    rss_pages: int = 7,
) -> str:
    fields = ["0"] * 50
    fields[0] = state
    fields[11] = str(user_ticks)
    fields[12] = str(system_ticks)
    fields[19] = str(start_ticks)
    fields[21] = str(rss_pages)
    return f"{pid} ({name}) " + " ".join(fields)


class FakeProcAdapter:
    def __init__(self, root: Path, rows: dict[int, dict[str, object]]):
        self.root = root
        self.rows = rows
        self.stat_reads: dict[int, int] = {}

    def list_pids(self):
        return sorted(self.rows)

    def exists(self, pid: int):
        return pid in self.rows

    def read_cmdline(self, pid: int):
        return tuple(self.rows[pid].get("cmdline", ()))

    def read_cwd(self, pid: int):
        value = self.rows[pid].get("cwd")
        return Path(value) if value is not None else None

    def read_stat(self, pid: int):
        values = self.rows[pid].get("stats")
        if not isinstance(values, list):
            value = self.rows[pid].get("stat")
            return value if isinstance(value, str) else None
        index = self.stat_reads.get(pid, 0)
        self.stat_reads[pid] = index + 1
        return values[min(index, len(values) - 1)]

    def read_status(self, pid: int):
        value = self.rows[pid].get("status")
        return value if isinstance(value, str) else None

    def read_io(self, pid: int):
        value = self.rows[pid].get("io")
        return value if isinstance(value, str) else None

    def count_fds(self, pid: int):
        return int(self.rows[pid].get("fds", 0))

    def task_stats(self, pid: int):
        return tuple(self.rows[pid].get("tasks", ()))

    def identity(self, pid: int):
        stat = self.read_stat(pid)
        parsed = load_process_module().parse_proc_stat(stat or "")
        if parsed is None:
            return None
        return {"pid": pid, "starttime_ticks": parsed["starttime_ticks"]}


def candidate(root: Path, *, cwd: Path | None = None, start_ticks: int = 100):
    return {
        "cmdline": (
            "python",
            "-m",
            "uvicorn",
            "app.main:app",
            "--workers",
            "1",
            "--token=TOP-SECRET",
        ),
        "cwd": cwd or root / "backend",
        "stat": proc_stat(101, start_ticks=start_ticks),
    }


def test_proc_adapter_reads_bounded_per_process_entries(tmp_path):
    process = load_process_module()
    proc_root = tmp_path / "proc"
    process_root = proc_root / "17"
    process_root.mkdir(parents=True)
    (process_root / "cmdline").write_bytes(b"python\0-m\0uvicorn\0app.main:app\0")
    (process_root / "cwd").symlink_to(tmp_path)
    (process_root / "stat").write_text(proc_stat(17))
    (process_root / "status").write_text("Threads:\t2\n")
    (process_root / "io").write_text("read_bytes: 4\n")
    (process_root / "fd").mkdir()
    (process_root / "fd" / "0").write_text("")
    task = process_root / "task" / "17"
    task.mkdir(parents=True)
    (task / "stat").write_text(proc_stat(17, state="D"))
    adapter = process.ProcAdapter(proc_root)
    assert adapter.list_pids() == (17,)
    assert adapter.exists(17) is True
    assert adapter.read_cmdline(17) == ("python", "-m", "uvicorn", "app.main:app")
    assert adapter.read_cwd(17) == tmp_path
    assert adapter.count_fds(17) == 1
    assert len(adapter.task_stats(17)) == 1
    assert adapter.identity(17) == {"pid": 17, "starttime_ticks": 100}
    assert adapter.read_cmdline(999) == ()


def test_resolve_backend_pid_requires_exactly_one_sanitized_candidate(tmp_path):
    process = load_process_module()
    root = tmp_path / "repo"
    root.mkdir()
    (root / "backend").mkdir()

    missing = process.resolve_backend_pid(root, proc=FakeProcAdapter(root, {}), self_pid=999)
    assert missing == {"status": "missing", "pid": None, "source": "auto", "candidates": []}

    one = process.resolve_backend_pid(
        root,
        proc=FakeProcAdapter(root, {101: candidate(root)}),
        self_pid=999,
    )
    assert one["status"] == "ok"
    assert one["pid"] == 101
    assert one["source"] == "auto"
    assert one["identity"] == {"pid": 101, "starttime_ticks": 100}
    assert one["candidates"] == [{"pid": 101, "cwd": "<repo>/backend"}]
    assert "TOP-SECRET" not in repr(one)

    rows = {
        101: candidate(root),
        202: candidate(root, cwd=root, start_ticks=200),
        303: candidate(root, cwd=tmp_path / "other"),
        404: {"cmdline": ("python", "server.py"), "cwd": root, "stat": proc_stat(404)},
        505: {
            "cmdline": ("python", "not-uvicorn", "prefix-app.main:app-suffix"),
            "cwd": root,
            "stat": proc_stat(505),
        },
        999: candidate(root),
    }
    ambiguous = process.resolve_backend_pid(
        root,
        proc=FakeProcAdapter(root, rows),
        self_pid=999,
    )
    assert ambiguous == {
        "status": "ambiguous",
        "pid": None,
        "source": "auto",
        "candidates": [
            {"pid": 101, "cwd": "<repo>/backend"},
            {"pid": 202, "cwd": "<repo>"},
        ],
    }
    assert "TOP-SECRET" not in repr(ambiguous)
    assert str(root) not in repr(ambiguous)


def test_explicit_pid_is_verified_without_command_matching(tmp_path):
    process = load_process_module()
    root = tmp_path / "repo"
    root.mkdir()
    row = {55: {"cmdline": ("custom", "secret"), "cwd": tmp_path, "stat": proc_stat(55)}}
    proc = FakeProcAdapter(root, row)
    selected = process.resolve_backend_pid(root, pid=55, proc=proc, self_pid=999)
    assert selected == {
        "status": "ok",
        "pid": 55,
        "source": "operator",
        "identity": {"pid": 55, "starttime_ticks": 100},
        "candidates": [],
    }
    assert process.resolve_backend_pid(root, pid=56, proc=proc, self_pid=999) == {
        "status": "missing",
        "pid": None,
        "source": "operator",
        "candidates": [],
    }


def test_pid_scan_race_degrades_to_missing_instead_of_escaping(tmp_path):
    process = load_process_module()

    class RacingProc:
        def list_pids(self):
            def values():
                yield 10
                raise PermissionError("PRIVATE PROC ERROR")

            return values()

    result = process.resolve_backend_pid(tmp_path, proc=RacingProc(), self_pid=999)
    assert result == {
        "status": "missing",
        "pid": None,
        "source": "auto",
        "candidates": [],
    }


def test_parse_stat_handles_spaces_and_parentheses_and_sample_is_bounded(tmp_path):
    process = load_process_module()
    root = tmp_path / "repo"
    row = {
        "stats": [
            proc_stat(42, name="worker (nested) name", user_ticks=100, system_ticks=20, start_ticks=25),
            proc_stat(42, name="worker (nested) name", user_ticks=125, system_ticks=25, start_ticks=25),
        ],
        "status": "Name:\tsecret-name\nState:\tS (sleeping)\nVmRSS:\t12 kB\nThreads:\t3\n",
        "io": "rchar: 999999\nread_bytes: 4096\nwrite_bytes: 8192\n",
        "fds": 9,
        "tasks": (
            proc_stat(42, state="S"),
            proc_stat(43, state="D"),
            proc_stat(44, state="D"),
        ),
    }
    proc = FakeProcAdapter(root, {42: row})
    moments = iter((10.0, 10.25))
    sample = process.sample_process(
        42,
        proc=proc,
        clock=lambda: next(moments),
        sleeper=lambda _seconds: None,
        clock_ticks=100,
        page_size=4096,
    )
    assert sample == {
        "pid": 42,
        "state": "S",
        "cpu_percent": 120.0,
        "rss_bytes": 12 * 1024,
        "threads": 3,
        "fds": 9,
        "read_bytes": 4096,
        "write_bytes": 8192,
        "d_state_threads": 2,
        "uptime_seconds": 10.25 - 0.25,
    }
    assert "secret-name" not in repr(sample)


def snapshot(pid: int, heartbeat: datetime):
    return {
        "schema_version": 1,
        "pid": pid,
        "process_started_at": heartbeat.isoformat(),
        "heartbeat_at": heartbeat.isoformat(),
        "last_state_change_at": heartbeat.isoformat(),
        "state_revision": 1,
        "snapshot_failures": 0,
        "readiness": {},
        "concurrency": {},
        "active_requests": [],
        "active_sql": [],
        "write_lock": {"holder": None, "waiters": []},
        "active_jobs": [],
        "recent_jobs": [],
    }


def test_load_runtime_snapshot_validates_shape_pid_schema_and_freshness(tmp_path):
    process = load_process_module()
    now = datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc)
    path = tmp_path / "runtime.json"

    path.write_text(json.dumps(snapshot(77, now - timedelta(seconds=2))))
    valid = process.load_runtime_snapshot(path, 77, now=now)
    assert valid["status"] == "ok"
    assert valid["fresh"] is True
    assert valid["age_seconds"] == 2.0
    assert valid["snapshot"]["pid"] == 77

    path.write_text("{malformed")
    assert process.load_runtime_snapshot(path, 77, now=now) == {
        "status": "malformed",
        "fresh": False,
        "snapshot": None,
    }

    path.write_text(json.dumps(snapshot(78, now)))
    assert process.load_runtime_snapshot(path, 77, now=now)["status"] == "pid_mismatch"

    wrong_schema = snapshot(77, now)
    wrong_schema["schema_version"] = 2
    path.write_text(json.dumps(wrong_schema))
    assert process.load_runtime_snapshot(path, 77, now=now)["status"] == "unsupported_schema"

    wrong_shape = snapshot(77, now)
    wrong_shape["active_requests"] = "private request body"
    path.write_text(json.dumps(wrong_shape))
    assert process.load_runtime_snapshot(path, 77, now=now)["status"] == "invalid_shape"

    path.write_text(json.dumps(snapshot(77, now - timedelta(seconds=6.01))))
    stale = process.load_runtime_snapshot(path, 77, now=now)
    assert stale["status"] == "stale"
    assert stale["fresh"] is False
    assert stale["age_seconds"] == pytest.approx(6.01)

    non_finite = snapshot(77, now)
    non_finite["readiness"] = {"latency": math.nan}
    path.write_text(json.dumps(non_finite))
    assert process.load_runtime_snapshot(path, 77, now=now)["status"] == "invalid_shape"


def test_probe_http_returns_metadata_only():
    process = load_process_module()

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, *_args):
            raise AssertionError("response bodies must never be read")

    result = process.probe_http(
        "backend",
        "http://127.0.0.1:8000/api/ready?token=SECRET",
        opener=lambda _url, timeout: Response(),
    )
    assert result["role"] == "backend"
    assert result["status"] == 200
    assert result["result"] == "ok"
    assert 0 <= result["elapsed_ms"] <= 750
    assert "url" not in result
    assert "SECRET" not in repr(result)
    assert "SENSITIVE" not in repr(result)


def test_probe_http_classifies_refused_connection_without_raw_error():
    process = load_process_module()
    def refused(_url, timeout):
        assert timeout == 0.75
        raise process.urllib.error.URLError(ConnectionRefusedError("PRIVATE"))

    result = process.probe_http(
        "frontend", "http://127.0.0.1:3000/", opener=refused
    )
    assert result["role"] == "frontend"
    assert result["status"] is None
    assert result["result"] == "refused"
    assert set(result) == {"role", "status", "elapsed_ms", "result"}


def test_parse_thread_dump_keeps_only_sanitized_bounded_frames(tmp_path):
    process = load_process_module()
    root = tmp_path / "repo"
    dump = f'''Thread 0x00000000000000AB (most recent call first):
  File "{root}/backend/app/service.py", line 17 in run
  File "/Users/private/secret_source.py", line 9 in leak
  File "/opt/python/lib/threading.py", line 1045 in _bootstrap_inner

Current thread 0x00000000000000CD (most recent call first):
  File "{root}/scripts/diag_process.py", line 200 in capture_thread_dump
  File "/private/site-packages/httpx/client.py", line 12 in send
  File "/Users/private/token-file.py", line 2 in hidden
  File "/Users/private/customer-openai-secret.py", line 3 in hidden
'''
    parsed = process.parse_thread_dump(dump, root)
    assert set(parsed) == {"ab", "cd"}
    rendered = repr(parsed)
    assert f"<repo>/backend/app/service.py:{17} in run" in rendered
    assert f"<library>/threading.py:{1045} in _bootstrap_inner" in rendered
    assert f"<library>/client.py:{12} in send" in rendered
    assert "<1 frame(s) omitted>" in rendered
    assert "<2 frame(s) omitted>" in rendered
    assert "/Users/" not in rendered
    assert str(root) not in rendered
    assert "secret_source" not in rendered
    assert "token-file" not in rendered
    assert "customer-openai-secret" not in rendered


def test_parse_thread_dump_caps_frames_per_thread(tmp_path):
    process = load_process_module()
    root = tmp_path / "repo"
    lines = ["Thread 0xA (most recent call first):"]
    lines.extend(
        f'  File "{root}/backend/app/service.py", line {index} in run'
        for index in range(1_000)
    )
    parsed = process.parse_thread_dump("\n".join(lines), root)
    assert len(parsed["a"]) == 65
    assert parsed["a"][-1] == "<936 frame(s) omitted>"
    assert len(repr(parsed).encode("utf-8")) < 16 * 1024


def test_capture_thread_dump_appends_only_keeps_child_alive_and_leaks_no_secrets(tmp_path):
    assert hasattr(signal, "SIGUSR1")
    process = load_process_module()
    code = r'''
import faulthandler
import signal
import sys
import threading
import time
from pathlib import Path

directory = Path(sys.argv[1])
handle = (directory / "thread-dumps.log").open("a", buffering=1)
faulthandler.register(signal.SIGUSR1, file=handle, all_threads=True, chain=False)
def worker():
    local_auth_token = "LOCAL-AUTH-TOKEN-MUST-NOT-LEAK"
    while local_auth_token:
        time.sleep(0.02)
threading.Thread(target=worker, daemon=True).start()
print("READY", flush=True)
while True:
    time.sleep(0.02)
'''
    dump_path = tmp_path / "thread-dumps.log"
    prefix = b"OLD-DUMP-MUST-NOT-BE-RETURNED\n"
    dump_path.write_bytes(prefix)
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path), "CMDLINE-TOKEN-MUST-NOT-LEAK"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "READY"
        result = process.capture_thread_dump(
            child.pid,
            tmp_path,
            platform="test-posix",
        )
        assert result["status"] == "ok"
        assert result["identity_verified"] is True
        assert child.poll() is None
        assert result["offset"] == len(prefix)
        assert "OLD-DUMP" not in result["dump"]
        assert "LOCAL-AUTH" not in result["dump"]
        assert "CMDLINE-TOKEN" not in result["dump"]
        parsed = process.parse_thread_dump(result["dump"], REPO)
        assert len(parsed) >= 2
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_capture_refuses_identity_change_before_signalling(tmp_path):
    process = load_process_module()
    dump = tmp_path / "thread-dumps.log"
    dump.write_bytes(b"")
    proc = FakeProcAdapter(tmp_path, {88: {"stat": proc_stat(88, start_ticks=9)}})
    called = []
    result = process.capture_thread_dump(
        88,
        tmp_path,
        proc=proc,
        expected_identity={"pid": 88, "starttime_ticks": 8},
        kill_fn=lambda *_args: called.append(True),
        platform="linux",
    )
    assert result["status"] == "identity_changed"
    assert result["identity_verified"] is False
    assert called == []


def test_capture_degrades_without_linux_runtime_or_platform_support(tmp_path):
    process = load_process_module()
    called = []
    unsupported = process.capture_thread_dump(
        os.getpid(),
        tmp_path,
        kill_fn=lambda *_args: called.append(True),
        platform="win32",
    )
    assert unsupported["status"] == "unsupported"
    missing_runtime = process.capture_thread_dump(
        os.getpid(),
        tmp_path,
        kill_fn=lambda *_args: called.append(True),
        platform="test-posix",
    )
    assert missing_runtime["status"] == "dump_unavailable"
    assert called == []


def test_linux_capture_requires_sigusr1_to_be_caught(tmp_path):
    process = load_process_module()
    (tmp_path / "thread-dumps.log").write_bytes(b"")
    proc = FakeProcAdapter(
        tmp_path,
        {
            88: {
                "stat": proc_stat(88, start_ticks=8),
                "status": "Name:\tpython\nSigCgt:\t0000000000000000\n",
            }
        },
    )
    called = []
    result = process.capture_thread_dump(
        88,
        tmp_path,
        proc=proc,
        expected_identity={"pid": 88, "starttime_ticks": 8},
        kill_fn=lambda *_args: called.append(True),
        platform="linux",
    )
    assert result["status"] == "signal_unavailable"
    assert called == []


def test_capture_lock_wait_honors_caller_deadline(tmp_path):
    module = load_process_module()
    assert module.fcntl is not None
    (tmp_path / "thread-dumps.log").write_bytes(b"")
    lock_path = tmp_path / "incident.lock"
    with lock_path.open("a+b") as held:
        module.fcntl.flock(held.fileno(), module.fcntl.LOCK_EX)
        started = time.monotonic()
        result = module.capture_thread_dump(
            os.getpid(), tmp_path, timeout=0.05, platform="test-posix"
        )
        elapsed = time.monotonic() - started
    assert result["status"] == "lock_timeout"
    assert elapsed < 0.2


def test_capture_rejects_dump_symlink_without_signalling(tmp_path):
    process = load_process_module()
    outside = tmp_path / "outside.log"
    outside.write_text("DO-NOT-MUTATE")
    diagnostics = tmp_path / "diagnostics"
    diagnostics.mkdir()
    (diagnostics / "thread-dumps.log").symlink_to(outside)
    called = []
    result = process.capture_thread_dump(
        os.getpid(),
        diagnostics,
        kill_fn=lambda *_args: called.append(True),
        platform="test-posix",
    )
    assert result["status"] == "unsafe_path"
    assert called == []
    assert outside.read_text() == "DO-NOT-MUTATE"


def test_capture_detects_pid_reuse_after_dump_and_discards_segment(tmp_path):
    process = load_process_module()
    dump = tmp_path / "thread-dumps.log"
    dump.write_bytes(b"")
    proc = FakeProcAdapter(
        tmp_path,
        {
            88: {
                "stats": [
                    proc_stat(88, start_ticks=8),
                    proc_stat(88, start_ticks=8),
                    proc_stat(88, start_ticks=9),
                ],
            }
        },
    )

    def append_dump(_pid, _signal):
        with dump.open("ab") as handle:
            handle.write(b"Thread 0xA (most recent call first):\n")

    result = process.capture_thread_dump(
        88,
        tmp_path,
        proc=proc,
        expected_identity={"pid": 88, "starttime_ticks": 8},
        kill_fn=append_dump,
        platform="test-posix",
    )
    assert result["status"] == "identity_changed"
    assert result["dump"] == ""


def test_standalone_main_emits_only_bounded_copy_safe_metadata(monkeypatch, capsys, tmp_path):
    process = load_process_module()
    monkeypatch.setattr(
        process,
        "resolve_backend_pid",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "pid": 7,
            "source": "operator",
            "identity": {"pid": 7, "starttime_ticks": 3},
            "candidates": [],
        },
    )
    monkeypatch.setattr(process, "sample_process", lambda _pid: {"pid": 7, "cpu_percent": 1.0})
    monkeypatch.setattr(
        process,
        "load_runtime_snapshot",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "fresh": True,
            "age_seconds": 1.0,
            "snapshot": {
                "active_requests": [{"authorization": "Bearer SECRET-TOKEN"}],
                "filename": "/home/private/customer-design.pdf",
                "padding": "密" * 40_000,
            },
        },
    )
    monkeypatch.setattr(
        process,
        "capture_thread_dump",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "identity_verified": True,
            "dump": "",
            "bytes": 0,
        },
    )
    monkeypatch.setattr(process, "parse_thread_dump", lambda *_args: {})
    monkeypatch.setattr(
        process,
        "probe_http",
        lambda role, _url: {"role": role, "status": 200, "elapsed_ms": 1, "result": "ok"},
    )
    assert process.main(["--root", str(tmp_path), "--pid", "7"]) == 0
    output = capsys.readouterr().out
    assert len(output.encode("utf-8")) <= 32_768
    assert "SECRET" not in output
    assert "customer-design" not in output
    assert "/home/" not in output
    assert "snapshot" not in output
