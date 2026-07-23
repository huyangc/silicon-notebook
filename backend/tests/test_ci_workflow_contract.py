"""GitHub Actions keeps offline and PostgreSQL gates isolated and least-privilege."""
from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"


def _load_workflow() -> dict[str, object]:
    workflow = yaml.load(
        WORKFLOW_PATH.read_text(encoding="utf-8"),
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


def test_offline_ci_job_installs_declared_dependencies_and_runs_only_complete_gate() -> None:
    workflow = _load_workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert set(jobs) == {"full-gate", "postgres-integration"}
    job = jobs["full-gate"]
    assert isinstance(job, dict)

    assert job["name"] == "full-gate"
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == "20"

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

    node = _uses_step(
        job,
        "actions/setup-node@48b55a011bda9f5d6aeb4c2d9c7362e8dae4041e",
    )
    assert node["with"] == {
        "node-version": "22",
        "cache": "npm",
        "cache-dependency-path": "frontend/package-lock.json",
    }

    steps = job["steps"]
    assert isinstance(steps, list)
    run_steps = [
        step
        for step in steps
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    ]
    commands = [step["run"] for step in run_steps]
    assert (
        "python -m pip install --no-cache-dir -r backend/requirements.txt"
        in commands
    )
    assert "npm ci --prefix frontend" in commands
    assert commands[-1] == "bash scripts/check.sh"
    assert [
        command for command in commands if "scripts/check" in command
    ] == ["bash scripts/check.sh"]

    gate = run_steps[-1]
    assert gate["env"] == {
        "PYTHON_BIN": "python",
        "BACKEND_PYTEST_WORKERS": "4",
    }


def test_postgres_ci_job_uses_pg16_least_privilege_targets_and_only_pg_gate() -> None:
    workflow = _load_workflow()
    job = workflow["jobs"]["postgres-integration"]
    assert isinstance(job, dict)
    assert job["name"] == "postgres-integration"
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == "25"

    services = job["services"]
    assert isinstance(services, dict)
    assert set(services) == {"postgres"}
    service = services["postgres"]
    assert service["image"] == "postgres:16"
    assert service["env"] == {
        "POSTGRES_USER": "postgres",
        "POSTGRES_PASSWORD": "ci-only-admin-password",
        "POSTGRES_DB": "postgres",
    }
    assert service["ports"] == ["5432:5432"]
    assert "pg_isready -U postgres -d postgres" in service["options"]

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
        "silicon_notebook_ci_test",
        "silicon_notebook_non_c_test",
        "LOCALE_PROVIDER icu ICU_LOCALE 'en-US'",
        "silicon_notebook_non_utf_test",
        "ENCODING 'SQL_ASCII'",
    ):
        assert phrase in command
    assert "print(" not in command

    gate = _named_step(job, "Run isolated PostgreSQL integration gate")
    assert gate["run"] == "bash scripts/check_postgres.sh"
    env = gate["env"]
    assert env["PYTHON_BIN"] == "python"
    assert env["POSTGRES_CI_AUXILIARY_TARGETS_REQUIRED"] == "1"
    for key in (
        "TEST_POSTGRES_URL",
        "TEST_POSTGRES_NON_C_URL",
        "TEST_POSTGRES_NON_UTF_URL",
    ):
        assert env[key].startswith(
            "postgresql://silicon_notebook_app:ci-only-app-password@127.0.0.1:5432/"
        )
        assert "postgres@" not in env[key]

    run_commands = [
        step["run"]
        for step in job["steps"]
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    ]
    assert [command for command in run_commands if "scripts/check" in command] == [
        "bash scripts/check_postgres.sh"
    ]


def test_ci_builds_hnswlib_portably_without_reusing_native_wheels() -> None:
    workflow = _load_workflow()
    job = workflow["jobs"]["full-gate"]
    assert isinstance(job, dict)

    python = _uses_step(
        job,
        "actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405",
    )
    assert python["with"] == {"python-version": "3.13"}

    install = _named_step(job, "Install backend dependencies")
    assert install["env"] == {"HNSWLIB_NO_NATIVE": "1"}
    assert install["run"] == (
        "python -m pip install --no-cache-dir -r backend/requirements.txt"
    )
