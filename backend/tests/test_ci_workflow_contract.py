"""GitHub Actions keeps offline and PostgreSQL gates isolated and least-privilege."""
from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
DAILY_EXTENDED_WORKFLOW_PATH = (
    ROOT / ".github" / "workflows" / "daily-extended.yml"
)


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


def test_standard_ci_job_installs_declared_dependencies_and_runs_only_standard_gate() -> None:
    workflow = _load_workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert set(jobs) == {
        "standard-gate",
        "frontend-node-current",
        "postgres-integration",
    }
    job = jobs["standard-gate"]
    assert isinstance(job, dict)

    assert job["name"] == "level-1-standard"
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


def test_frontend_node_current_job_covers_the_documented_node_ceiling() -> None:
    """前端泳道必须在 Node.js **当前**大版本上再跑一遍,而不是只跑 G1 钉的 22。

    两份 README 与部署文档承诺「Node.js ≥ 20」,而 `standard-gate` 钉 22 ——
    没有这条泳道,承诺的上半段就无人验证。真机踩过:Node ≥ 24 自带 Web Storage
    全局,不给 `--localstorage-file` 时 getter 返回 `undefined`,vitest 的 jsdom
    环境会让它盖住 jsdom 自己那份,于是每一条读 `localStorage` 的组件测试都在
    开发者本机红、CI 全绿(修复见 `frontend/test-support/setup.ts`)。

    这条泳道**同时**跑生产构建:那次修复的第一版误引了没有类型声明的 `jsdom`,
    正是 `next build` 的类型检查当场抓到的。只跑 `npm test` 就漏掉那一类。
    """
    workflow = _load_workflow()
    job = workflow["jobs"]["frontend-node-current"]
    assert isinstance(job, dict)
    assert job["name"] == "level-1-frontend-node26"
    assert job["runs-on"] == "ubuntu-24.04"

    checkout = _uses_step(
        job,
        "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    )
    assert checkout["with"] == {"persist-credentials": "false"}

    node = _uses_step(
        job,
        "actions/setup-node@48b55a011bda9f5d6aeb4c2d9c7362e8dae4041e",
    )
    # 这条泳道的**全部意义**就是这个版本高于 standard-gate 钉的 22:两者相等时
    # 它只是把同一份验证跑了两遍,那个缺口会重新变成不可见。
    standard_node = _uses_step(
        workflow["jobs"]["standard-gate"],
        "actions/setup-node@48b55a011bda9f5d6aeb4c2d9c7362e8dae4041e",
    )
    assert int(node["with"]["node-version"]) > int(
        standard_node["with"]["node-version"]
    )
    assert node["with"]["cache"] == "npm"
    assert node["with"]["cache-dependency-path"] == "frontend/package-lock.json"

    commands = [
        step["run"]
        for step in job["steps"]
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    ]
    assert commands == [
        "npm ci --prefix frontend",
        "npm test --prefix frontend",
        "npm run build --prefix frontend",
    ]
    # 这条泳道只碰前端:后端门禁归 standard-gate,重复跑既慢又会让「哪条门禁在管
    # 什么」变糊。
    assert not any("scripts/check" in command for command in commands)


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
        "silicon_notebook_ci_owner_decoy",
        "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB",
        "GRANT {} TO {}",
        "silicon_notebook_ci_test",
        "silicon_notebook_non_c_test",
        "LOCALE_PROVIDER icu ICU_LOCALE 'en-US'",
        "silicon_notebook_non_utf_test",
        "ENCODING 'SQL_ASCII'",
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
    job = workflow["jobs"]["standard-gate"]
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


def test_daily_extended_gate_runs_once_per_day_and_is_manually_dispatchable() -> None:
    workflow = _load_workflow_path(DAILY_EXTENDED_WORKFLOW_PATH)

    assert workflow["name"] == "Daily Extended Gate"
    events = workflow["on"]
    assert isinstance(events, dict)
    assert events == {
        "schedule": [{"cron": "17 18 * * *"}],
        "workflow_dispatch": {},
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "daily-extended-${{ github.ref }}",
        "cancel-in-progress": "true",
    }

    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert set(jobs) == {"extended-gate"}
    job = jobs["extended-gate"]
    assert isinstance(job, dict)
    assert job["name"] == "level-2-extended"
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == "25"

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

    run_steps = [
        step
        for step in job["steps"]
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    ]
    commands = [step["run"] for step in run_steps]
    assert commands[-1] == "bash scripts/check_extended.sh"
    assert [command for command in commands if "scripts/check" in command] == [
        "bash scripts/check_extended.sh"
    ]
    assert run_steps[-1]["env"] == {
        "PYTHON_BIN": "python",
        "BACKEND_PYTEST_WORKERS": "4",
    }
