"""GitHub Actions keeps offline and PostgreSQL gates isolated and least-privilege."""
from __future__ import annotations

import ast
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.child_env_testkit import BYTECODE_ENV

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
DAILY_EXTENDED_WORKFLOW_PATH = (
    ROOT / ".github" / "workflows" / "daily-extended.yml"
)
# Every job installs the two hash-locked files (runtime, then test tools) and
# never resolves: upgrading a dependency is scripts/lock_python_deps.sh, not CI.
LOCKED_INSTALL = (
    "python -m pip install --require-hashes"
    " -r backend/requirements.txt -r backend/requirements-dev.txt"
)
DAILY_LOCKED_INSTALL = LOCKED_INSTALL.replace(
    "pip install --require-hashes", "pip install --no-cache-dir --require-hashes"
)
LOCK_FILES_HASH = "hashFiles('backend/requirements.txt', 'backend/requirements-dev.txt')"


def _load_workflow() -> dict[str, object]:
    return _load_workflow_path(WORKFLOW_PATH)


def _load_workflow_path(path: Path) -> dict[str, object]:
    workflow = yaml.load(
        path.read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert isinstance(workflow, dict)
    return workflow


def _uses_step(job: dict[str, object], action: str) -> dict[str, object]:
    steps = job["steps"]
    assert isinstance(steps, list)
    matches = [
        step
        for step in steps
        if isinstance(step, dict) and step.get("uses") == action
    ]
    assert len(matches) == 1
    return matches[0]


def _named_step(job: dict[str, object], name: str) -> dict[str, object]:
    steps = job["steps"]
    assert isinstance(steps, list)
    matches = [
        step
        for step in steps
        if isinstance(step, dict) and step.get("name") == name
    ]
    assert len(matches) == 1
    return matches[0]


def test_ci_events_permissions_and_concurrency_are_bounded() -> None:
    workflow = _load_workflow()

    assert workflow["name"] == "CI"
    events = workflow["on"]
    assert isinstance(events, dict)
    assert set(events) == {"pull_request", "push", "workflow_dispatch"}
    assert events["pull_request"] == {"branches": ["master"]}
    assert events["push"] == {"branches": ["master"]}
    assert events["workflow_dispatch"] == {}
    assert "pull_request_target" not in events

    assert workflow["permissions"] == {"contents": "read"}
    concurrency = workflow["concurrency"]
    assert isinstance(concurrency, dict)
    assert concurrency["cancel-in-progress"] == "true"
    assert "github.workflow" in concurrency["group"]
    assert "github.event.pull_request.head.ref" in concurrency["group"]
    assert "github.ref" in concurrency["group"]
    assert "secrets." not in repr(workflow)



def test_postgres_ci_job_uses_pg16_least_privilege_targets_and_only_pg_gate() -> None:
    workflow = _load_workflow()
    job = workflow["jobs"]["postgres-integration"]
    assert isinstance(job, dict)
    assert job["name"] == "level-3-postgres-integration"
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == "35"

    services = job["services"]
    assert isinstance(services, dict)
    assert set(services) == {"postgres"}
    service = services["postgres"]
    assert service["image"] == "postgres:16"
    assert service["env"] == {
        "POSTGRES_USER": "postgres",
        "POSTGRES_PASSWORD": "ci-only-admin-password",
        "POSTGRES_DB": "postgres",
        "POSTGRES_INITDB_ARGS": "--set=shared_buffers=512MB --set=max_wal_size=2GB",
    }
    assert service["ports"] == ["5432:5432"]
    assert "pg_isready -U postgres -d postgres" in service["options"]
    assert (
        "--mount type=tmpfs,destination=/var/lib/postgresql/data,tmpfs-size=6442450944"
        in service["options"]
    )

    checkout = _uses_step(
        job,
        "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    )
    assert checkout["with"] == {"persist-credentials": "false"}
    python = _uses_step(
        job,
        "actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405",
    )
    assert python["with"] == {"python-version": "3.13"}

    provision = _named_step(job, "Provision least-privilege PostgreSQL test targets")
    command = provision["run"]
    for phrase in (
        "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION",
        "silicon_notebook_ci_owner_decoy",
        "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB",
        "GRANT {} TO {}",
        "for worker in range(4)",
        'database = f"silicon_notebook_{kind}_w{worker}_test"',
        "LOCALE_PROVIDER icu ICU_LOCALE 'en-US'",
        "ENCODING 'SQL_ASCII'",
        'os.environ["GITHUB_OUTPUT"]',
        '"fsync", "synchronous_commit", "full_page_writes"',
        "SELECT current_setting(%s)",
    ):
        assert phrase in command
    assert "print(" not in command

    gate = _named_step(job, "Run PostgreSQL adapter integration gate")
    assert gate["run"] == "bash scripts/check_postgres.sh"
    env = gate["env"]
    assert env["PYTHON_BIN"] == "python"
    assert env["POSTGRES_CI_AUXILIARY_TARGETS_REQUIRED"] == "1"
    assert env["TEST_POSTGRES_DECOY_OWNER_ROLE"] == (
        "silicon_notebook_ci_owner_decoy"
    )
    assert env["TEST_POSTGRES_TARGETS_JSON"] == (
        "${{ steps.postgres-targets.outputs.targets }}"
    )
    # Serial and parallel target sets cannot be mixed: the launcher fails
    # closed instead of guessing which database the caller intended.
    assert not set(env).intersection({
        "TEST_POSTGRES_URL",
        "TEST_POSTGRES_NON_C_URL",
        "TEST_POSTGRES_NON_UTF_URL",
    })

    cache = _named_step(job, "Cache portable Python dependencies")
    assert cache["uses"] == "actions/cache@0057852bfaa89a56745cba8c7296529d2fc39830"
    # Job-level env is evaluated before runner context is available.
    assert job["env"]["PIP_CACHE_DIR"] == "${{ github.workspace }}/.local/pip-portable-v1"
    assert cache["with"]["path"] == "${{ env.PIP_CACHE_DIR }}"
    assert "portable-hnsw-v1" in cache["with"]["key"]
    assert "runner.arch" in cache["with"]["key"]
    assert LOCK_FILES_HASH in cache["with"]["key"]
    assert "restore-keys" not in cache["with"]
    install = _named_step(job, "Install backend dependencies")
    assert install["env"] == {"HNSWLIB_NO_NATIVE": "1"}
    assert install["run"] == LOCKED_INSTALL

    timings = _named_step(job, "Upload PostgreSQL test timings")
    assert timings["if"] == "always()"
    assert set(timings["with"]["path"].splitlines()) == {
        "backend/.local/postgres-junit.xml", "backend/.local/postgres-metrics.json",
    }

    run_commands = [
        step["run"]
        for step in job["steps"]
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    ]
    assert [command for command in run_commands if "scripts/check" in command] == [
        "bash scripts/check_postgres.sh"
    ]


def _ci_provisioning_functions():
    job = _load_workflow()["jobs"]["postgres-integration"]
    command = _named_step(job, "Provision least-privilege PostgreSQL test targets")["run"]
    source = command.removeprefix("python - <<'PY'\n").removesuffix("PY\n")
    module = ast.parse(source)
    functions = [node for node in module.body if isinstance(node, ast.FunctionDef)]
    assert {function.name for function in functions} == {
        "configure_ci_logging", "verify_ci_resources",
    }
    connection_blocks = [
        node for node in module.body if isinstance(node, ast.With)
        and any(isinstance(item.optional_vars, ast.Name)
                and item.optional_vars.id == "connection" for item in node.items)
    ]
    assert len(connection_blocks) == 1
    # Both guards run before any CREATE ROLE/database side effect, rather than
    # merely being defined somewhere in the provisioning program.
    for invocation, name in zip(
        connection_blocks[0].body, ("configure_ci_logging", "verify_ci_resources"),
    ):
        assert isinstance(invocation, ast.Expr) and isinstance(invocation.value, ast.Call)
        assert isinstance(invocation.value.func, ast.Name)
        assert invocation.value.func.id == name
    namespace = {"time": SimpleNamespace(sleep=lambda _: None)}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(WORKFLOW_PATH), "exec"), namespace)
    return namespace


def _ci_logging_configurator():
    return _ci_provisioning_functions()["configure_ci_logging"]


def test_postgres_ci_initdb_budgets_and_diagnostics_are_bounded() -> None:
    job = _load_workflow()["jobs"]["postgres-integration"]
    service = job["services"]["postgres"]
    # The official entrypoint expands this string as initdb arguments. Reject
    # additional server options that could weaken real migrations or isolation.
    assert shlex.split(service["env"]["POSTGRES_INITDB_ARGS"]) == [
        "--set=shared_buffers=512MB", "--set=max_wal_size=2GB",
    ]
    options = shlex.split(service["options"])
    assert options[options.index("--mount") + 1] == (
        "type=tmpfs,destination=/var/lib/postgresql/data,tmpfs-size=6442450944"
    )
    assert not {"--memory", "--memory-swap"}.intersection(options)
    storage = _named_step(job, "Record PostgreSQL storage usage")
    assert storage["if"] == "always()"
    for diagnostic in (
        "df -h", "/sys/fs/cgroup/memory.peak", "/sys/fs/cgroup/cpu.stat",
        "checkpoints_req", "buffers_backend", "shared_buffers_bytes",
        "max_wal_size_bytes", "pg_stat_wal", "wal_bytes", "wal_buffers_full",
    ):
        assert diagnostic in storage["run"]


@pytest.mark.parametrize(
    ("override", "accepted"),
    [
        ({}, True),
        ({"shared_buffers": 128 * 1024**2}, False),
        ({"max_wal_size": 1024**3}, False),
        ({"fsync": "off"}, False),
        ({"synchronous_commit": "off"}, False),
        ({"full_page_writes": "off"}, False),
    ],
)
def test_postgres_ci_rejects_unapplied_budgets_and_weakened_durability(
    override, accepted,
) -> None:
    settings = {
        "shared_buffers": 512 * 1024**2,
        "max_wal_size": 2 * 1024**3,
        "fsync": "on", "synchronous_commit": "on", "full_page_writes": "on",
        **override,
    }
    checked = []

    def execute(statement, params):
        setting, = params
        assert statement == (
            "SELECT pg_size_bytes(current_setting(%s))"
            if setting in {"shared_buffers", "max_wal_size"}
            else "SELECT current_setting(%s)"
        )
        checked.append(setting)
        return SimpleNamespace(fetchone=lambda: (settings[setting],))

    verify = _ci_provisioning_functions()["verify_ci_resources"]
    connection = SimpleNamespace(execute=execute)
    if accepted:
        verify(connection)
        assert set(checked) == set(settings)
    else:
        bad_setting, = override
        with pytest.raises(RuntimeError, match=bad_setting):
            verify(connection)
        assert checked[-1] == bad_setting


class _CiLoggingServer:
    """Reload affects later sessions; the administrative session stays stale."""

    def __init__(self, *, reload_ok=True, effective_after=2):
        self.reload_ok = reload_ok
        self.effective_after = effective_after
        self.pending = None
        self.reloads = 0
        self.probes = 0
        self.closed_probes = 0

    def execute(self, statement):
        match = re.fullmatch(r"ALTER SYSTEM SET (\w+) = '(\w+)'", statement)
        if match:
            # Only repeated SQL bodies may be suppressed: errors, diagnostics,
            # message severity and the application's privileges stay intact.
            assert match[1] == "log_min_error_statement"
            self.pending = match[2]
            return None
        assert statement == "SELECT pg_reload_conf()"
        self.reloads += 1
        return SimpleNamespace(fetchone=lambda: (self.reload_ok,))

    def connect(self):
        self.probes += 1
        effective = (
            self.pending if self.reloads and self.probes >= self.effective_after else "error"
        )
        server = self

        class Probe:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                server.closed_probes += 1

            def execute(self, statement):
                assert statement == "SHOW log_min_error_statement"
                return SimpleNamespace(fetchone=lambda: (effective,))

        return Probe()


def test_postgres_ci_logging_reloads_and_verifies_fresh_sessions() -> None:
    server = _CiLoggingServer()
    _ci_logging_configurator()(server, server.connect)
    assert server.pending == "panic"
    assert server.reloads == 1
    assert server.probes == server.closed_probes == 2


@pytest.mark.parametrize("reload_ok", [False, True])
def test_postgres_ci_logging_fails_closed_if_reload_is_not_effective(reload_ok) -> None:
    server = _CiLoggingServer(reload_ok=reload_ok, effective_after=1000)
    with pytest.raises(RuntimeError, match="logging policy"):
        _ci_logging_configurator()(server, server.connect)
    assert server.reloads == 1
    assert server.probes == server.closed_probes == (50 if reload_ok else 0)


@pytest.mark.parametrize(
    "job_name", ["standard-backend", "standard-contracts-frontend", "postgres-integration"],
)
def test_ci_builds_hnswlib_portably_without_reusing_native_wheels(job_name) -> None:
    workflow = _load_workflow()
    job = workflow["jobs"][job_name]
    assert isinstance(job, dict)

    python = _uses_step(
        job,
        "actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405",
    )
    assert python["with"] == {"python-version": "3.13"}

    install = _named_step(job, "Install backend dependencies")
    assert install["env"] == {"HNSWLIB_NO_NATIVE": "1"}
    assert install["run"] == LOCKED_INSTALL
    assert job["env"]["PIP_CACHE_DIR"] == "${{ github.workspace }}/.local/pip-portable-v1"
    cache = _uses_step(job, "actions/cache@0057852bfaa89a56745cba8c7296529d2fc39830")
    assert cache["with"] == {
        "path": "${{ env.PIP_CACHE_DIR }}",
        "key": "${{ runner.os }}-${{ runner.arch }}-py313-portable-hnsw-v1-${{ "
        + LOCK_FILES_HASH
        + " }}",
    }


PR_JOBS = {
    "standard-backend": "level-1-backend-primary",
    "standard-contracts-frontend": "level-1-backend-rest-contracts-frontend",
    "postgres-integration": "level-3-postgres-integration",
}
DAILY_JOBS = {
    "extended-backend": "level-2-backend-primary",
    "extended-contracts-frontend": "level-2-backend-rest-contracts-frontend",
    "extended-complement": "level-2-complement",
}
MAX_JOBS_PER_WORKFLOW = 3
G1_REMAINING_LANES_COMMAND = "bash scripts/check.sh --lane contracts --lane frontend"
BACKEND_SHARD_COMMANDS = [
    f"bash scripts/check_backend.sh --shard-index {index} --shard-count 2"
    for index in range(2)
]
BACKEND_SHARD_STEPS = [
    "Run standard backend primary shard", "Run standard backend remaining shard",
]
NODE_MAJOR = "26"
SETUP_NODE = "actions/setup-node@48b55a011bda9f5d6aeb4c2d9c7362e8dae4041e"


def _standard_gate_lanes() -> list[str]:
    """The lanes check.sh (G1) runs, read from its own LANES array."""
    source = (ROOT / "scripts" / "check.sh").read_text(encoding="utf-8")
    matches = re.findall(r"^declare -a LANES=\(([^)]*)\)$", source, re.MULTILINE)
    assert len(matches) == 1
    return matches[0].split()


def _run_commands(job: dict[str, object]) -> list[str]:
    return [
        step["run"]
        for step in job["steps"]
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    ]


def _lanes_run_by(command: str) -> set[str]:
    """Which G1 lanes one workflow command runs."""
    if command == "bash scripts/check.sh":
        return set(_standard_gate_lanes())
    if command in BACKEND_SHARD_COMMANDS:
        return {"backend"}
    if command.startswith("bash scripts/check.sh "):
        words = command.split()[2:]
        assert words[0::2] == ["--lane"] * (len(words) // 2), command
        return set(words[1::2])
    if command.startswith("bash scripts/check_") and command.endswith(".sh"):
        lane = command.removeprefix("bash scripts/check_").removesuffix(".sh")
        return {lane} if lane in _standard_gate_lanes() else set()
    return set()


@pytest.mark.parametrize(
    ("path", "expected"),
    [(WORKFLOW_PATH, PR_JOBS), (DAILY_EXTENDED_WORKFLOW_PATH, DAILY_JOBS)],
)
def test_each_workflow_has_at_most_three_jobs_and_no_summary_job(path, expected) -> None:
    """Every job shows up as a check on a PR, so each workflow keeps at most
    three and none of them only summarizes the others: a job's name says what
    it gates, and every job must pass."""
    jobs = _load_workflow_path(path)["jobs"]
    assert len(jobs) <= MAX_JOBS_PER_WORKFLOW
    assert {name: job["name"] for name, job in jobs.items()} == expected
    for name, job in jobs.items():
        assert "needs" not in job, name
        assert "strategy" not in job, name
        assert job["runs-on"] == "ubuntu-24.04"
        assert "if" not in job and "continue-on-error" not in job
        assert all("continue-on-error" not in step for step in job["steps"])
        checkout = _uses_step(
            job, "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
        )
        assert checkout["with"] == {"persist-credentials": "false"}
        if any("pip install" in command for command in _run_commands(job)):
            python = _uses_step(
                job, "actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405",
            )
            assert python["with"] == {"python-version": "3.13"}
        if any("npm ci" in command for command in _run_commands(job)):
            _uses_step(job, SETUP_NODE)


def test_pr_ci_runs_every_g1_lane_in_two_jobs() -> None:
    jobs = _load_workflow()["jobs"]

    backend = jobs["standard-backend"]
    assert backend["timeout-minutes"] == "30"
    assert _run_commands(backend) == [
        LOCKED_INSTALL,
        BACKEND_SHARD_COMMANDS[0],
    ]
    assert _named_step(backend, BACKEND_SHARD_STEPS[0])["env"] == {
        "PYTHON_BIN": "python", "BACKEND_PYTEST_WORKERS": "4",
    }

    rest = jobs["standard-contracts-frontend"]
    assert rest["timeout-minutes"] == "30"
    assert _run_commands(rest) == [
        LOCKED_INSTALL,
        "npm ci --prefix frontend",
        BACKEND_SHARD_COMMANDS[1],
        G1_REMAINING_LANES_COMMAND,
    ]
    assert _named_step(rest, "Run standard contracts and frontend lanes")["env"] == {
        "PYTHON_BIN": "python",
    }

    assert all("if" not in step for step in backend["steps"] if "run" in step)
    for step in rest["steps"]:
        if "run" not in step:
            continue
        if step["name"] in (BACKEND_SHARD_STEPS[1], "Run standard contracts and frontend lanes"):
            # These used to run on different jobs. Keep both reachable when
            # the other shard, npm install, or a prior test command fails;
            # failure still fails the job because continue-on-error is banned.
            assert step["if"] == "${{ !cancelled() }}"
        else:
            assert "if" not in step
    shard_commands = []
    for index, job in enumerate((backend, rest)):
        shard = _named_step(job, BACKEND_SHARD_STEPS[index])
        assert shard["env"] == {"PYTHON_BIN": "python", "BACKEND_PYTEST_WORKERS": "4"}
        words = shlex.split(shard["run"])
        assert words[:2] == ["bash", "scripts/check_backend.sh"]
        assert words[2::2] == ["--shard-index", "--shard-count"]
        assert words[5] == "2"
        shard_commands.append(int(words[3]))
    assert sorted(shard_commands) == [0, 1]
    covered = [
        lane
        for job in (backend, rest)
        for command in _run_commands(job)
        for lane in sorted(_lanes_run_by(command))
    ]
    # Backend is partitioned into exactly two complementary collections; the
    # real collection proof lives in test_test_architecture_policy.py. Every
    # other lane is still executed once and only once, after shard 1 finishes.
    assert sorted(covered) == sorted([*_standard_gate_lanes(), "backend"])

    for job_name, step_name, junit, metrics, artifact in (
        ("standard-backend", "Upload backend test timings", "backend-shard0-junit.xml",
         "backend-shard0-metrics.json", "backend-test-timings-shard-0"),
        ("standard-contracts-frontend", "Upload remaining backend test timings",
         "backend-shard1-junit.xml", "backend-shard1-metrics.json", "backend-test-timings-shard-1"),
        ("standard-contracts-frontend", "Upload contract test timings", "contracts-junit.xml",
         None, "contracts-test-timings"),
    ):
        upload = _named_step(jobs[job_name], step_name)
        assert upload["uses"] == "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
        assert upload["if"] == "always()"
        assert upload["with"]["name"] == artifact
        expected_paths = {f"backend/.local/{junit}"}
        if metrics:
            expected_paths.add(f"backend/.local/{metrics}")
        assert set(upload["with"]["path"].splitlines()) == expected_paths
        assert upload["with"]["retention-days"] == "7"


@pytest.mark.process_contract
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ((), ["backend", "contracts", "frontend"]),
        (("--lane", "frontend", "--lane", "contracts"), ["contracts", "frontend"]),
        (("--lane", "backend"), ["backend"]),
    ],
)
def test_check_sh_runs_all_lanes_or_exactly_the_selected_ones(
    arguments, expected, tmp_path,
) -> None:
    """Execute the real check.sh against recording stand-in lanes."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "check.sh").write_text(
        (ROOT / "scripts" / "check.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    calls = tmp_path / "calls"
    calls.mkdir()
    for lane in _standard_gate_lanes():
        stub = scripts / f"check_{lane}.sh"
        stub.write_text(f'#!/bin/sh\ntouch "{calls / lane}"\n', encoding="utf-8")
        stub.chmod(0o755)

    completed = subprocess.run(
        ["bash", str(scripts / "check.sh"), *arguments],
        env={
            "PATH": os.defpath, "PYTHON_BIN": sys.executable,
            "TMPDIR": str(tmp_path), **BYTECODE_ENV,
        },
        capture_output=True, text=True, timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert sorted(path.name for path in calls.iterdir()) == expected
    printed = [
        line.strip("= ") for line in completed.stdout.splitlines()
        if line.startswith("===== ") and line.strip("= ") in _standard_gate_lanes()
    ]
    # Printed in check.sh's own LANES order, whatever order --lane named them.
    assert printed == [lane for lane in _standard_gate_lanes() if lane in expected]


@pytest.mark.process_contract
@pytest.mark.parametrize("arguments", [("--lane",), ("--lane", "postgres"), ("backend",)])
def test_check_sh_rejects_unknown_lanes_and_arguments(arguments, tmp_path) -> None:
    completed = subprocess.run(
        ["bash", str(ROOT / "scripts" / "check.sh"), *arguments],
        env={
            "PATH": os.defpath, "PYTHON_BIN": sys.executable,
            "TMPDIR": str(tmp_path), **BYTECODE_ENV,
        },
        capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 2
    assert list(tmp_path.iterdir()) == []


def test_every_node_setup_uses_the_single_pinned_major() -> None:
    """One Node.js major everywhere: .nvmrc is the single pin, every CI Node
    setup reads it, and npm refuses any other major (engines + engine-strict)."""
    assert (ROOT / ".nvmrc").read_text(encoding="utf-8") == f"{NODE_MAJOR}\n"
    engines = {"node": f">={NODE_MAJOR} <{int(NODE_MAJOR) + 1}"}
    package = json.loads((ROOT / "frontend/package.json").read_text(encoding="utf-8"))
    assert package["engines"] == engines
    lock = json.loads(
        (ROOT / "frontend/package-lock.json").read_text(encoding="utf-8")
    )
    assert lock["packages"][""]["engines"] == engines
    npmrc = (ROOT / "frontend/.npmrc").read_text(encoding="utf-8").splitlines()
    assert "engine-strict=true" in npmrc

    setups = 0
    for path in (WORKFLOW_PATH, DAILY_EXTENDED_WORKFLOW_PATH):
        assert "node-version:" not in path.read_text(encoding="utf-8")
        for job in _load_workflow_path(path)["jobs"].values():
            for step in job["steps"]:
                if str(step.get("uses", "")).startswith("actions/setup-node@"):
                    assert step["uses"] == SETUP_NODE
                    assert step["with"] == {
                        "node-version-file": ".nvmrc",
                        "cache": "npm",
                        "cache-dependency-path": "frontend/package-lock.json",
                    }
                    setups += 1
    assert setups == 3


@pytest.mark.parametrize(
    "script", ["check_frontend.sh", "check_sample_plugin.sh", "pack.sh"],
)
def test_node_scripts_refuse_another_node_major(script) -> None:
    source = (ROOT / "scripts" / script).read_text(encoding="utf-8")
    assert 'source "$ROOT_DIR/scripts/node_version.sh"' in source
    assert "require_node_major " in source


@pytest.mark.process_contract
@pytest.mark.parametrize(
    ("version", "accepted"),
    [
        (f"v{NODE_MAJOR}.7.0", True), (NODE_MAJOR, True), ("v22.22.0", False),
        (f"v{NODE_MAJOR}0.1.0", False), ("", False),
    ],
)
def test_require_node_major_accepts_only_the_pinned_major(version, accepted) -> None:
    completed = subprocess.run(
        [
            "bash", "-c",
            'source "$1"; require_node_major "$2" "probe"',
            "_", str(ROOT / "scripts/node_version.sh"), version,
        ],
        env={"PATH": os.defpath, **BYTECODE_ENV},
        capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == (0 if accepted else 1)
    assert (f"probe needs Node.js {NODE_MAJOR}.x" in completed.stderr) == (not accepted)


def test_daily_extended_gate_runs_once_per_day_and_is_manually_dispatchable() -> None:
    workflow = _load_workflow_path(DAILY_EXTENDED_WORKFLOW_PATH)

    assert workflow["name"] == "Daily Extended Gate"
    assert workflow["on"] == {
        "schedule": [{"cron": "17 18 * * *"}],
        "workflow_dispatch": {},
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "daily-extended-${{ github.ref }}",
        "cancel-in-progress": "true",
    }
    assert "secrets." not in repr(workflow)
    for job in workflow["jobs"].values():
        # G2 keeps pip's wheel cache off (a cached -march=native hnswlib wheel
        # can SIGILL on another runner), so no daily job restores one.
        assert not any(
            str(step.get("uses", "")).startswith("actions/cache@")
            for step in job["steps"]
        )
        assert "env" not in job
        if any("pip install" in command for command in _run_commands(job)):
            install = _named_step(job, "Install backend dependencies")
            assert install["env"] == {"HNSWLIB_NO_NATIVE": "1"}
            assert install["run"] == DAILY_LOCKED_INSTALL


def test_daily_extended_gate_runs_every_g1_lane_exactly_as_pr_ci_does() -> None:
    """G2 is G1 plus a complement; the daily G1 half must be PR CI's G1, not a
    copy that drifts. Only the pip install differs (G2 keeps the wheel cache
    off); the contracts-and-frontend job then also forces the frontend lane's
    parallel path, which PR CI never takes."""
    daily = _load_workflow_path(DAILY_EXTENDED_WORKFLOW_PATH)["jobs"]
    ci = _load_workflow()["jobs"]

    def same_install(commands: list[str]) -> list[str]:
        return [
            DAILY_LOCKED_INSTALL if command == LOCKED_INSTALL else command
            for command in commands
        ]

    assert daily["extended-backend"]["timeout-minutes"] == "30"
    assert _run_commands(daily["extended-backend"]) == same_install(
        _run_commands(ci["standard-backend"])
    )
    assert _named_step(daily["extended-backend"], BACKEND_SHARD_STEPS[0])["env"] == (
        _named_step(ci["standard-backend"], BACKEND_SHARD_STEPS[0])["env"]
    )

    rest = daily["extended-contracts-frontend"]
    assert rest["timeout-minutes"] == "30"
    assert _run_commands(rest) == [
        *same_install(_run_commands(ci["standard-contracts-frontend"])),
        "bash scripts/check_frontend.sh",
    ]
    lanes = _named_step(rest, "Run standard contracts and frontend lanes")
    assert lanes["env"] == _named_step(
        ci["standard-contracts-frontend"], "Run standard contracts and frontend lanes",
    )["env"]
    assert lanes["if"] == "${{ !cancelled() }}"
    assert _named_step(rest, BACKEND_SHARD_STEPS[1])["env"] == (
        _named_step(ci["standard-contracts-frontend"], BACKEND_SHARD_STEPS[1])["env"]
    )
    assert _named_step(rest, BACKEND_SHARD_STEPS[1])["if"] == "${{ !cancelled() }}"
    for daily_name, ci_name in (
        ("extended-backend", "standard-backend"),
        ("extended-contracts-frontend", "standard-contracts-frontend"),
    ):
        # Every timing artifact mirrors PR CI, with a separate daily namespace.
        for step in ci[ci_name]["steps"]:
            if "upload-artifact@" not in step.get("uses", ""):
                continue
            counterpart = _named_step(daily[daily_name], step["name"])
            assert counterpart["uses"] == step["uses"]
            assert counterpart["if"] == step["if"] == "always()"
            assert counterpart["with"] == {
                **step["with"], "name": "daily-" + step["with"]["name"],
            }
    parallel = _named_step(rest, "Run frontend lane on its parallel path")
    assert parallel["env"] == {"FRONTEND_LANE_PARALLEL": "1"}
    # Runs after a failed G1 step too, so one daily run reports both paths.
    assert parallel["if"] == "${{ !cancelled() }}"

    # PR CI keeps its timing: no PR workflow job forces the parallel path.
    for name, ci_job in ci.items():
        for step in ci_job.get("steps", []):
            assert "FRONTEND_LANE_PARALLEL" not in (step.get("env") or {}), name


def test_daily_extended_complement_job_runs_only_the_complement_wrapper() -> None:
    job = _load_workflow_path(DAILY_EXTENDED_WORKFLOW_PATH)["jobs"][
        "extended-complement"
    ]
    assert job["name"] == "level-2-complement"
    commands = _run_commands(job)
    assert commands == [
        DAILY_LOCKED_INSTALL,
        "npm ci --prefix frontend",
        "bash scripts/check_extended.sh --complement-only",
    ]
    assert _named_step(job, "Run level-2 complement")["env"] == {
        "PYTHON_BIN": "python",
        "BACKEND_PYTEST_WORKERS": "4",
    }
    upload = _uses_step(
        job, "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
    )
    assert upload["if"] == "always()"
    assert set(upload["with"]["path"].splitlines()) == {
        "backend/.local/backend-extended-junit.xml",
        "backend/.local/backend-extended-metrics.json",
    }
    assert upload["with"]["retention-days"] == "7"


@pytest.mark.process_contract
@pytest.mark.parametrize("arguments", [(), ("--complement-only",)])
def test_check_extended_runs_g1_first_unless_the_workflow_runs_it_elsewhere(
    arguments, tmp_path,
) -> None:
    """Execute the real check_extended.sh against recording stand-ins.

    Without arguments it is the whole of G2 (check.sh, then the complement in
    order); --complement-only drops exactly the check.sh step and nothing else.
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "check_extended.sh").write_text(
        (ROOT / "scripts" / "check_extended.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    calls = tmp_path / "calls.txt"
    for name in ("check.sh", "check_backend_extended.sh", "check_sample_plugin.sh"):
        (scripts / name).write_text(
            f'echo "{name}" >> "{calls}"\n', encoding="utf-8",
        )
    fake_python = tmp_path / "python"
    fake_python.write_text(
        f'#!/bin/sh\necho "python $(basename "$1") $2" >> "{calls}"\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    completed = subprocess.run(
        ["bash", str(scripts / "check_extended.sh"), *arguments],
        env={"PATH": os.defpath, "PYTHON_BIN": str(fake_python), **BYTECODE_ENV},
        capture_output=True, text=True, timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    complement = [
        "check_backend_extended.sh",
        "check_sample_plugin.sh",
        "python audit_facade_callers.py --assert-no-retire-now",
    ]
    expected = complement if arguments else ["check.sh", *complement]
    assert calls.read_text(encoding="utf-8").splitlines() == expected


@pytest.mark.process_contract
def test_check_extended_rejects_unknown_arguments(tmp_path) -> None:
    completed = subprocess.run(
        ["bash", str(ROOT / "scripts" / "check_extended.sh"), "--shard-index"],
        env={"PATH": os.defpath, "PYTHON_BIN": str(tmp_path / "absent"), **BYTECODE_ENV},
        capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == 2
    assert "Unknown argument: --shard-index" in completed.stderr
