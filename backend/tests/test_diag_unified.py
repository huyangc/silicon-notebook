"""Tests for the unified diagnostics entry ``scripts/diag.py``.

``diag.py`` is a thin dispatcher over the seven canonical diagnostic commands:

  - ``incident`` -> bounded live capture (stdlib, app-free)
  - ``slow`` -> historical slow-path report (stdlib, app-free)
  - ``latency`` -> ask-stage percentiles (stdlib, app-free)
  - ``locks`` -> SQLite write-lock wait/hold distribution by call site
    (self-contained stdlib aggregation of db_write_lock_slow /
    db_write_lock_stats; no separate engine file)
  - ``open`` -> notebook-open latency evidence (stdlib, app-free)
  - ``db`` -> source-side-effect-free SQLite evidence (stdlib, app-free)
  - ``base-recall`` -> source-side-effect-free base-reference metadata (stdlib, app-free)

Contract under test:
  1. The command surface is exactly seven commands, with no obsolete aliases.
  2. All seven host-side commands never import ``app``.
  3. Every command has bounded help that works without cwd/PYTHONPATH assumptions.
  4. Bare/leading-flag invocation remains the compatibility alias for ``slow``.
  5. ``latency`` remains equivalent to the canonical application aggregation.
"""
from __future__ import annotations

import ast
import importlib.util
import gzip
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _REPO_ROOT / "scripts"
_DIAG = _SCRIPTS / "diag.py"
_COMMANDS = ("incident", "slow", "latency", "locks", "open", "db", "base-recall")
_OFFLINE_COMMANDS = _COMMANDS
# Repository evidence from `git show HEAD:scripts/diag.py`: these were the
# complete pre-Task 9 unified keys.  All remain approved, so no obsolete alias
# needs compatibility treatment or an invented name.
_PRE_TASK9_COMMANDS = frozenset(("slow", "latency", "base-recall"))
_OBSOLETE_COMMANDS = tuple(sorted(_PRE_TASK9_COMMANDS.difference(_COMMANDS)))


def _load_diag():
    """Import scripts/diag.py as a module (scripts/ is not a package)."""
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location("diag", _DIAG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_ask_stage(stage: str, latency_ms: int) -> dict:
    return {"kind": "ask_stage", "stage": stage, "latency_ms": latency_ms,
            "notebook_id": "nb-test", "ts": "2026-07-13T00:00:00", "channel": "events"}


def _without_layout_whitespace(value: str) -> str:
    """Undo argparse line wrapping while preserving every non-space byte."""
    return "".join(value.split())


def _assert_paths_absent(result: subprocess.CompletedProcess, *paths: Path) -> None:
    combined = _without_layout_whitespace(result.stdout + result.stderr)
    for path in paths:
        raw = Path(os.path.abspath(os.fspath(path)))
        resolved = Path(os.path.realpath(os.fspath(raw)))
        for candidate in {raw, resolved}:
            assert _without_layout_whitespace(os.fspath(candidate)) not in combined


def _help_command_section(output: str) -> tuple[str, ...]:
    _, section = output.split("子命令:\n", 1)
    rows = section.split("\n\n", 1)[0].splitlines()
    return tuple(
        row.strip().split(maxsplit=1)[0]
        for row in rows
        if len(row) - len(row.lstrip()) == 4
    )


# ---------------------------------------------------------------------------
# 1. file exists & dispatches
# ---------------------------------------------------------------------------

def test_diag_file_exists():
    assert _DIAG.is_file(), "scripts/diag.py must exist"


def test_dispatch_table_has_exactly_seven_subcommands():
    diag = _load_diag()
    assert tuple(diag.SUBCOMMANDS) == _COMMANDS
    assert set(diag.SUBCOMMANDS) == set(_COMMANDS)


@pytest.mark.parametrize("command", _COMMANDS)
def test_every_subcommand_help_is_cwd_and_pythonpath_independent(tmp_path, command):
    spaced_checkout = tmp_path / "checkout with spaces"
    spaced_checkout.symlink_to(_REPO_ROOT, target_is_directory=True)
    diag_path = spaced_checkout / "scripts" / "diag.py"
    cwd = tmp_path / "cwd with spaces"
    cwd.mkdir()
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, str(diag_path), command, "--help"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()
    assert len(result.stdout.encode("utf-8")) <= 32 * 1024
    _assert_paths_absent(result, spaced_checkout, _REPO_ROOT, cwd)


def test_top_level_help_lists_exactly_the_seven_commands(tmp_path):
    result = subprocess.run(
        [sys.executable, str(_DIAG), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert _help_command_section(result.stdout) == _COMMANDS
    assert not set(_OBSOLETE_COMMANDS).intersection(result.stdout.split())


def test_pre_task9_obsolete_commands_are_absent_and_rejected():
    assert _OBSOLETE_COMMANDS == ()
    for command in _OBSOLETE_COMMANDS:
        result = subprocess.run(
            [sys.executable, str(_DIAG), command],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode == 2
        assert command not in _help_command_section(result.stderr)


def test_unknown_subcommand_fails_clean():
    r = subprocess.run([sys.executable, str(_DIAG), "no-such-cmd"],
                       capture_output=True, text=True)
    assert r.returncode == 2
    assert "no-such-cmd" not in (r.stderr + r.stdout)
    assert "未知子命令" in (r.stderr + r.stdout)


# ---------------------------------------------------------------------------
# 2. latency aggregation == canonical ask_latency (drift guard)
# ---------------------------------------------------------------------------

def test_latency_aggregation_matches_ask_latency():
    from app.eval.ask_latency import aggregate_stage_latencies

    diag = _load_diag()
    records = (
        [_make_ask_stage("score", v) for v in [10, 20, 30, 40, 100]]
        + [_make_ask_stage("answer_llm", v) for v in [1000, 2000, 3000]]
        + [_make_ask_stage("total", v) for v in [1234]]
        + [{"kind": "other", "x": 1}]  # non ask_stage ignored
    )
    assert diag._aggregate_stage(records) == aggregate_stage_latencies(records)


def test_latency_subcommand_end_to_end(tmp_path):
    log = tmp_path / "events.jsonl"
    recs = [_make_ask_stage("score", v) for v in [10, 20, 30, 40, 100]]
    log.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")

    r = subprocess.run([sys.executable, str(_DIAG), "latency", "--log", str(log)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "score" in r.stdout
    # p50 of [10,20,30,40,100] nearest-rank ceiling -> 30
    assert "30.0" in r.stdout


def test_latency_reads_per_user_subdirs(tmp_path):
    """Mirror ask_latency: global file + per-user subdir files are merged."""
    (tmp_path / "events.jsonl").write_text(
        json.dumps(_make_ask_stage("score", 10)) + "\n", encoding="utf-8")
    sub = tmp_path / "user-abc"
    sub.mkdir()
    (sub / "events.jsonl").write_text(
        json.dumps(_make_ask_stage("score", 90)) + "\n", encoding="utf-8")

    diag = _load_diag()
    records = list(diag._read_ask_stage(str(tmp_path / "events.jsonl"), None))
    latencies = sorted(r["latency_ms"] for r in records)
    assert latencies == [10, 90], "must aggregate global + per-user subdir logs"


def test_latency_reads_rotated_gzip_file_once(tmp_path):
    """A daily gzip log is sufficient input for the offline latency command."""
    with gzip.open(tmp_path / "events-2026-07-13.jsonl.gz", "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(_make_ask_stage("score", 42)) + "\n")

    r = subprocess.run(
        [sys.executable, str(_DIAG), "latency", "--log", str(tmp_path / "events.jsonl")],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("score") == 1


# ---------------------------------------------------------------------------
# 3. offline purity — no heavy app import
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", _OFFLINE_COMMANDS)
@pytest.mark.parametrize("help_requested", (False, True), ids=("run", "help"))
def test_offline_subcommands_do_not_import_any_app_module(
    tmp_path, command, help_requested
):
    log = tmp_path / "events.jsonl"
    log.write_text(json.dumps(_make_ask_stage("score", 5)) + "\n", encoding="utf-8")
    (tmp_path / ".local" / "logs").mkdir(parents=True)
    args = [command, "--help"] if help_requested else {
        "incident": [
            "incident", "--root", str(tmp_path), "--local", str(tmp_path),
            "--pid", "2147483647",
        ],
        "slow": ["slow", "--root", str(tmp_path)],
        "latency": ["latency", "--log", str(log)],
        "locks": ["locks", "--log", str(log)],
        "open": ["open", "--local", str(tmp_path)],
        "db": ["db", "--db", str(tmp_path / "missing.db")],
        "base-recall": [
            "base-recall", "--db", str(tmp_path / "missing.db"),
            "--deadline-seconds", "0.01",
        ],
    }[command]
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(_SCRIPTS)!r})\n"
        "import diag\n"
        "try:\n"
        f"    exit_code = int(diag.main({args!r}) or 0)\n"
        "except SystemExit as exc:\n"
        "    exit_code = int(exc.code or 0)\n"
        "bad = [name for name in sys.modules "
        "if name == 'app' or name.startswith('app.')]\n"
        "raise SystemExit('IMPORTED APP: ' + repr(bad) if bad else exit_code)\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=tmp_path, timeout=15)
    assert r.returncode == 0, f"offline subcommand imported app: {r.stderr or r.stdout}"


def test_bare_and_leading_flag_slow_do_not_import_app(tmp_path):
    (tmp_path / ".local" / "logs").mkdir(parents=True)
    for args in ([], ["--root", str(tmp_path)]):
        code = (
            "import sys;"
            f"sys.path.insert(0, {str(_SCRIPTS)!r});"
            "import diag;"
            f"diag.main({args!r});"
            "bad=[m for m in sys.modules if m == 'app' or m.startswith('app.')];"
            "sys.exit('IMPORTED APP: '+repr(bad) if bad else 0)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr or result.stdout
        assert "慢因诊断" in result.stdout


def test_importing_diag_does_not_import_app():
    """Merely importing the dispatcher must not pull any application module."""
    code = (
        "import sys;"
        f"sys.path.insert(0, {str(_SCRIPTS)!r});"
        "import diag;"
        "bad=[m for m in sys.modules if m == 'app' or m.startswith('app.')];"
        "sys.exit('IMPORTED APP: '+repr(bad) if bad else 0)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=str(_REPO_ROOT))
    assert r.returncode == 0, r.stderr or r.stdout


def test_dispatcher_dependency_contract_keeps_all_seven_handlers_app_free():
    tree = ast.parse(_DIAG.read_text(encoding="utf-8"), filename=str(_DIAG))
    sibling_imports = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("_cmd_"):
            continue
        sibling_imports[node.name] = {
            child.names[0].name
            for child in ast.walk(node)
            if isinstance(child, ast.Import)
            and child.names[0].name.startswith("diag_")
        }
    assert sibling_imports == {
        "_cmd_incident": {"diag_incident"},
        "_cmd_slow": {"diag_slow"},
        "_cmd_latency": set(),
        "_cmd_locks": set(),
        "_cmd_open": {"diag_open_latency"},
        "_cmd_db": {"diag_db"},
        "_cmd_base_recall": {"diag_base_report"},
    }
    assert all(
        not import_name.startswith("app")
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for import_name in (
            [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
        )
    )


@pytest.mark.parametrize(
    "script_name",
    (
        "diag_incident.py",
        "diag_slow.py",
        "diag_open_latency.py",
        "diag_db.py",
        "diag_base_report.py",
    ),
)
def test_stdlib_engine_scripts_remain_directly_runnable_from_any_cwd(
    tmp_path, script_name
):
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, str(_SCRIPTS / script_name), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()


def test_slow_bare_defaults_to_slow(tmp_path):
    (tmp_path / ".local" / "logs").mkdir(parents=True)
    r = subprocess.run([sys.executable, str(_DIAG), "--root", str(tmp_path)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "慢因诊断" in r.stdout


def _privacy_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "ABSOLUTE-SENSITIVE-ROOT"
    local = root / ".local"
    logs = local / "logs"
    logs.mkdir(parents=True)
    event_rows = [
        {
            "kind": "ask_stage",
            "stage": f"SENSITIVE-RAW-STAGE-{index}",
            "latency_ms": float("nan") if index == 0 else index,
            "notebook_id": "nb-SENSITIVE-PRIVATE-ID",
            "ts": "2026-07-22T00:00:00",
            "channel": "events",
        }
        for index in range(2500)
    ]
    (logs / "events.jsonl").write_text(
        "\n".join(json.dumps(row) for row in event_rows)
        + "\n{SENSITIVE-MALFORMED-CONTENT\n",
        encoding="utf-8",
    )
    (logs / "requests.jsonl").write_text(
        json.dumps({
            "kind": "http",
            "method": "GET",
            "path": "/api/notebooks/nb-SENSITIVE-PRIVATE-ID/sources/src-SENSITIVE-CONTENT",
            "latency_ms": 9000,
            "status": 200,
            "ts": "2026-07-22T00:00:00",
            "channel": "requests",
        }) + "\n",
        encoding="utf-8",
    )
    (logs / "llm.jsonl").write_text(
        json.dumps({
            "kind": "model_error",
            "model": "SENSITIVE-MODEL-CONTENT",
            "error": "Bearer SENSITIVE-AUTH-TOKEN",
            "latency_ms": 6000,
            "ts": "2026-07-22T00:00:00",
            "channel": "llm",
        }) + "\n",
        encoding="utf-8",
    )
    (root / ".env").write_text(
        "PORT=8000\nOPENAI_COMPAT_API_KEY=SENSITIVE-ENV-SECRET\n",
        encoding="utf-8",
    )
    database = local / "silicon_notebook.db"
    database.write_bytes(b"")
    return root, local, logs / "events.jsonl"


def _command_matrix(root: Path, local: Path, events: Path):
    missing_db = local / "silicon_notebook.db"
    unified = {
        "unified-incident": [_DIAG, "incident", "--root", root, "--local", local, "--pid", "2147483647"],
        "unified-slow": [_DIAG, "slow", "--root", root, "--local", local],
        "unified-latency": [_DIAG, "latency", "--log", events],
        "unified-locks": [_DIAG, "locks", "--log", events],
        "unified-open": [_DIAG, "open", "--root", root, "--local", local, "--notebook", "nb-SENSITIVE-PRIVATE-ID"],
        "unified-db": [_DIAG, "db", "--db", missing_db, "--notebook-id", "nb-SENSITIVE-PRIVATE-ID"],
        "unified-base-recall": [_DIAG, "base-recall", "--db", missing_db, "--notebook-id", "nb-SENSITIVE-PRIVATE-ID"],
    }
    direct = {
        "direct-incident": [_SCRIPTS / "diag_incident.py", "--root", root, "--local", local, "--pid", "2147483647"],
        "direct-slow": [_SCRIPTS / "diag_slow.py", "--root", root, "--local", local],
        "direct-open": [_SCRIPTS / "diag_open_latency.py", "--root", root, "--local", local, "--notebook", "nb-SENSITIVE-PRIVATE-ID"],
        "direct-db": [_SCRIPTS / "diag_db.py", "--db", missing_db, "--notebook-id", "nb-SENSITIVE-PRIVATE-ID"],
        "direct-base-recall": [_SCRIPTS / "diag_base_report.py", "--db", missing_db, "--notebook-id", "nb-SENSITIVE-PRIVATE-ID"],
    }
    return {**unified, **direct}


def test_all_seven_commands_and_direct_engines_share_privacy_and_byte_envelope(tmp_path):
    root, local, events = _privacy_fixture(tmp_path)
    for name, raw_args in _command_matrix(root, local, events).items():
        args = [sys.executable, *(os.fspath(value) for value in raw_args)]
        result = subprocess.run(
            args,
            cwd=tmp_path,
            capture_output=True,
            timeout=15,
        )
        assert result.returncode == 0, (name, result.stderr.decode("utf-8", "replace"))
        combined = result.stdout + result.stderr
        combined.decode("utf-8", "strict")
        assert result.stdout.endswith(b"\n"), name
        assert len(combined) <= 32 * 1024, name
        for sentinel in (
            b"SENSITIVE",
            b"nb-SENSITIVE-PRIVATE-ID",
            b"Bearer",
            os.fsencode(root),
            os.fsencode(local),
        ):
            assert sentinel not in combined, (name, sentinel, combined[:1000])
        assert b"Traceback" not in combined, name
        if name.endswith("slow"):
            assert "慢因诊断".encode() in combined, name
            assert b"diagnostic_error=unavailable" not in combined, name


@pytest.mark.parametrize("surface", ("unified-open", "direct-open"))
def test_open_report_uses_per_report_pseudonym_for_arbitrary_notebook_id(tmp_path, surface):
    root, local, events = _privacy_fixture(tmp_path)
    notebook_id = "customerNotebookAlpha"
    database = local / "silicon_notebook.db"
    with sqlite3.connect(database) as conn:
        conn.executescript(
            "CREATE TABLE knowledge_objects ("
            "notebook_id TEXT, object_type TEXT, status TEXT, updated_at TEXT, source_id TEXT);"
            "CREATE TABLE sources ("
            "notebook_id TEXT, id TEXT, created_at TEXT, parse_status TEXT);"
        )
        conn.execute(
            "INSERT INTO knowledge_objects VALUES (?, 'concept', 'approved', '', '')",
            (notebook_id,),
        )
        conn.execute("INSERT INTO sources VALUES (?, 'source-one', '', 'parsed')", (notebook_id,))

    command = _command_matrix(root, local, events)[surface]
    command = [notebook_id if os.fspath(value) == "nb-SENSITIVE-PRIVATE-ID" else value
               for value in command]
    result = subprocess.run(
        [sys.executable, *(os.fspath(value) for value in command)],
        cwd=tmp_path,
        capture_output=True,
        timeout=15,
    )

    combined = result.stdout + result.stderr
    assert result.returncode == 0
    assert notebook_id.encode() not in combined
    assert b"notebook#1" in combined


@pytest.mark.parametrize("surface", ("unified-open", "direct-open", "unified-slow", "direct-slow"))
def test_sqlite_exception_is_fixed_category_only_on_unified_and_direct_surfaces(
    tmp_path, surface
):
    root, local, events = _privacy_fixture(tmp_path)
    injection = tmp_path / "inject"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(
        "import sqlite3\n"
        "def fail(*args, **kwargs):\n"
        "    raise sqlite3.OperationalError('SENSITIVE-SQLITE-EXCEPTION /absolute/private Bearer token')\n"
        "sqlite3.connect = fail\n",
        encoding="utf-8",
    )
    command = _command_matrix(root, local, events)[surface]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.fspath(injection)
    result = subprocess.run(
        [sys.executable, *(os.fspath(value) for value in command)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=15,
    )
    combined = result.stdout + result.stderr
    combined.decode("utf-8", "strict")
    assert result.returncode in {0, 1}
    assert result.stdout.endswith(b"\n")
    assert len(combined) <= 32 * 1024
    assert b"SENSITIVE" not in combined
    assert b"Bearer" not in combined
    assert b"/absolute/private" not in combined
    assert b"Traceback" not in combined
    assert b"sqlite_error" in combined or b"diagnostic_error=unavailable" in combined

# ---------------------------------------------------------------------------
# 4. locks: offline db_write_lock_slow / db_write_lock_stats aggregation
# ---------------------------------------------------------------------------

def test_locks_subcommand_aggregates_write_lock_events(tmp_path, capsys):
    """diag.py locks 从 events.jsonl 离线聚合写锁事件。纯 stdlib、零 app 依赖。

    site a.py:1 故意给 100 条各异样本(而不是两条恰好让 max/p99 重合的样本)——
    _p99 是 nearest-rank ceiling,样本数 < 100 时 p99 必然等于 max(ceil(0.99n)
    对 n<100 恒等于 n),这种小样本巧合无法把「聚合」和「不聚合、每条原始事件各
    自一行」两种实现区分开(两种情形下 "1200" 都会出现在输出里)。100 条样本
    (hold_ms=1000..1099)才会让 hold_p99(=1098.0,第 99 百分位,index 98)真正
    不同于 hold_max(=1099.0);再加上断言 a.py:1 只出现一行、且该行的 n 恰好是
    100(而不是「一条原始事件一行」退化情形下隐含的 1),两者合起来才是「确实按
    site 聚合了」的不可伪造证明:一旦回归成不聚合,这里会看到 100 行而不是 1
    行,且每行 n=1、hold_max==hold_p99(单样本必然相等),当场被戳穿。
    """
    import json
    import sys

    log = tmp_path / "events.jsonl"
    a_rows = [
        {"kind": "db_write_lock_slow", "site": "a.py:1",
         "wait_ms": 10.0 + i, "hold_ms": 1000.0 + i}
        for i in range(100)
    ]
    rows = a_rows + [
        {"kind": "db_write_lock_slow", "site": "b.py:2",
         "wait_ms": 300.0, "hold_ms": 10.0},
        {"kind": "ask_stage", "stage": "retrieve", "ms": 3},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                   encoding="utf-8")

    sys.path.insert(0, str(_REPO_ROOT / "scripts"))
    import diag
    rc = diag.main(["locks", "--log", str(log)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "b.py:2" in out
    assert "ask_stage" not in out  # 不相关事件被过滤

    a_lines = [ln for ln in out.splitlines() if ln.startswith("a.py:1")]
    assert len(a_lines) == 1, "必须聚合成一行,而非每条原始事件各自一行"
    _site, n, wait_max, hold_max, wait_p99, hold_p99 = a_lines[0].split()
    assert n == "100"            # 聚合后的样本数,不是退化情形下的 1
    assert wait_max == "109.0" and wait_p99 == "108.0"
    assert hold_max == "1099.0" and hold_p99 == "1098.0"
    assert wait_max != wait_p99 and hold_max != hold_p99  # 100 样本才会让 p99 落在最大值之外


def test_locks_subcommand_is_registered(tmp_path):
    import sys
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))
    import diag
    assert "locks" in diag.SUBCOMMANDS


def test_locks_reads_per_user_subdirs(tmp_path, capsys):
    """镜像 test_latency_reads_per_user_subdirs:写锁事件只落在 per-user 子目录时,
    `locks` 子命令必须仍能看到它。这正是加这个测试的原因 —— WriteLockStats 的周期性
    `db_write_lock_stats` 快照走 EventLogger(per_user=True),会落进「触发那次 flush
    时恰好活跃的用户」的子目录,而不是固定写全局文件;只读单个 --log 文件会静默漏掉
    大部分数据。这里刻意不写全局文件,只在 subdir 里放数据,使回归(若 locks 退化回
    单文件读取)必定表现为「完全看不到」而不是「看到一部分」。两种 kind 都覆盖到:
    db_write_lock_slow(违规表)与 db_write_lock_stats(全量快照表)。"""
    import json
    import sys

    log = tmp_path / "events.jsonl"  # 全局文件不存在
    sub = tmp_path / "user-abc"
    sub.mkdir()
    sub_rows = [
        {"kind": "db_write_lock_slow", "site": "c.py:9",
         "wait_ms": 400.0, "hold_ms": 900.0},
        {"kind": "db_write_lock_stats",
         "sites": {"c.py:9": {"count": 42, "wait_max_ms": 410.0,
                               "hold_max_ms": 900.0, "wait_p99_ms": 405.0,
                               "hold_p99_ms": 880.0}}},
    ]
    (sub / "events.jsonl").write_text(
        "\n".join(json.dumps(r) for r in sub_rows) + "\n", encoding="utf-8")

    sys.path.insert(0, str(_REPO_ROOT / "scripts"))
    import diag
    rc = diag.main(["locks", "--log", str(log)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "c.py:9" in out
    assert "900" in out  # 违规表的 hold_max,来自 subdir 里的 db_write_lock_slow
    assert "42" in out   # 快照表的 count,来自 subdir 里的 db_write_lock_stats


def test_locks_subcommand_does_not_import_app(tmp_path):
    """locks 必须和 latency/slow 一样保持零 app 依赖。不加进上面已有的
    test_offline_subcommands_do_not_import_app 的 parametrize 列表 —— 那样会在
    既有测试前面插入一行,挪动它和它后面所有测试的行号(见文件顶部关于
    line-number-sensitive 架构守卫的说明);单独追加一个测试,只在文件末尾加行。"""
    import json
    log = tmp_path / "events.jsonl"
    log.write_text(json.dumps(
        {"kind": "db_write_lock_slow", "site": "x.py:1",
         "wait_ms": 1.0, "hold_ms": 1.0}) + "\n", encoding="utf-8")
    code = (
        "import sys;"
        f"sys.path.insert(0, {str(_SCRIPTS)!r});"
        "import diag;"
        f"diag.main(['locks', '--log', {str(log)!r}]);"
        "bad=[m for m in ('app.core.config','app.services.sqlite_repository') if m in sys.modules];"
        "sys.exit('IMPORTED APP: '+repr(bad) if bad else 0)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=str(_REPO_ROOT))
    assert r.returncode == 0, f"locks subcommand imported app: {r.stderr or r.stdout}"


# ---------------------------------------------------------------------------
# 5. locks: the completeness caveat must actually reach the reader, in both
#    places it is supposed to live (the printed footnote and --help).
# ---------------------------------------------------------------------------

def test_locks_output_includes_caveat_footnote(tmp_path, capsys):
    """The `locks` subcommand prints a caveat footnote under its tables (see
    _cmd_locks) explaining that db_write_lock_slow is a rate-limited sample
    and db_write_lock_stats reflects only one worker's view under multiple
    processes — previously asserted nowhere. The footnote is unconditional
    (printed regardless of whether either table has rows), so an empty log
    is enough to exercise it."""
    import sys
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))
    import diag

    log = tmp_path / "events.jsonl"
    log.write_text("", encoding="utf-8")
    rc = diag.main(["locks", "--log", str(log)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "为限流抽样(非全量)" in out
    assert "只反映其中一个 worker 的视角" in out


def test_locks_help_mentions_same_caveat(tmp_path):
    """The same caveat is also meant to surface in `--help` (see _cmd_locks's
    argparse `description`) — previously asserted nowhere either."""
    r = subprocess.run([sys.executable, str(_DIAG), "locks", "--help"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "按 site 限流的样本(非全量)" in r.stdout
    assert "只是" in r.stdout and "worker 的视角" in r.stdout
