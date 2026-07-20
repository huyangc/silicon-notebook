"""GitHub Actions must remain a read-only wrapper around the complete gate."""
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


def test_ci_job_installs_declared_dependencies_and_runs_only_the_complete_gate() -> None:
    workflow = _load_workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert set(jobs) == {"full-gate"}
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
    assert python["with"] == {
        "python-version": "3.13",
        "cache": "pip",
        "cache-dependency-path": "backend/requirements.txt",
    }

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
    assert "python -m pip install -r backend/requirements.txt" in commands
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
