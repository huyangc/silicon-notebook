"""GitHub Actions keeps offline and PostgreSQL gates isolated and least-privilege."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest
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


def test_standard_ci_lanes_keep_all_wrappers_and_cover_both_backend_shards() -> None:
    workflow = _load_workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert set(jobs) == {
        "standard-gate",
        "standard-backend",
        "standard-contracts",
        "standard-frontend",
        "frontend-node-current",
        "postgres-integration",
    }
    for lane in ("backend", "contracts", "frontend"):
        job = jobs[f"standard-{lane}"]
        assert job["runs-on"] == "ubuntu-24.04"
        assert job["timeout-minutes"] == "20"
        assert "if" not in job and "continue-on-error" not in job
        checkout = _uses_step(
            job, "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
        )
        assert checkout["with"] == {"persist-credentials": "false"}
        commands = [step["run"] for step in job["steps"] if "run" in step]
        expected_gate = f"bash scripts/check_{lane}.sh"
        if lane == "backend":
            expected_gate += " --shard-index ${{ matrix.shard }} --shard-count 2"
        assert commands == [
            "npm ci --prefix frontend" if lane == "frontend" else
            "python -m pip install -r backend/requirements.txt",
            expected_gate,
        ]
        assert all("if" not in step and "continue-on-error" not in step
                   for step in job["steps"] if "run" in step)
        if lane != "frontend":
            python = _uses_step(
                job, "actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405",
            )
            assert python["with"] == {"python-version": "3.13"}
            upload = _uses_step(
                job, "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
            )
            assert upload["if"] == "always()"
            assert upload["with"]["retention-days"] == "7"
            assert upload["with"]["path"] == (
                "backend/.local/backend-junit-shard-${{ matrix.shard }}.xml"
                if lane == "backend" else "backend/.local/contracts-junit.xml"
            )
    assert jobs["standard-backend"]["strategy"] == {
        "fail-fast": "false", "matrix": {"shard": ["0", "1"]},
    }
    assert _named_step(jobs["standard-backend"], "Run standard backend shard")["env"] == {
        "PYTHON_BIN": "python", "BACKEND_PYTEST_WORKERS": "4",
    }
    assert _named_step(jobs["standard-contracts"], "Run standard contracts lane")["env"] == {
        "PYTHON_BIN": "python",
    }
    node = _uses_step(
        jobs["standard-frontend"],
        "actions/setup-node@48b55a011bda9f5d6aeb4c2d9c7362e8dae4041e",
    )
    assert node["with"] == {
        "node-version": "22",
        "cache": "npm",
        "cache-dependency-path": "frontend/package-lock.json",
    }


@pytest.mark.parametrize("lane", ["standard-backend", "standard-contracts", "standard-frontend"])
@pytest.mark.parametrize("result", ["success", "failure", "cancelled", "skipped", None])
def test_standard_aggregate_executes_fail_closed_for_every_lane(lane, result, tmp_path) -> None:
    """Execute the actual workflow command; skipped/missing jobs must never look green."""
    job = _load_workflow()["jobs"]["standard-gate"]
    expected = {"standard-backend", "standard-contracts", "standard-frontend"}
    assert job["name"] == "level-1-standard"
    assert set(job["needs"]) == expected
    assert job["if"] == "${{ always() }}"
    assert "continue-on-error" not in job
    assert len(job["steps"]) == 1
    gate = job["steps"][0]
    assert "if" not in gate and "continue-on-error" not in gate
    assert gate["env"] == {"NEEDS_JSON": "${{ toJSON(needs) }}"}
    needs = {name: {"result": "success"} for name in expected}
    if result is None:
        del needs[lane]
    else:
        needs[lane]["result"] = result
    completed = subprocess.run(
        ["bash", "-e", "-c", gate["run"]], cwd=tmp_path,
        env={"PATH": os.defpath, "NEEDS_JSON": json.dumps(needs)},
        capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode == (0 if result == "success" else 1), completed.stderr
    assert ("Every standard lane passed." in completed.stdout) == (result == "success")


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
        workflow["jobs"]["standard-frontend"],
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
    # 必须**委派**给 G1 用的同一个 wrapper,而不是把 `npm test`/`npm run build` 抄进
    # workflow:抄一遍的话,wrapper 将来加一步前置检查,这条泳道会继续绿着跑一份旧清单、
    # 与 G1 和本地验证悄悄分叉。这条泳道要换的只有 Node 版本。
    assert commands == [
        "npm ci --prefix frontend",
        "bash scripts/check_frontend.sh",
    ]
    # 只碰前端:后端门禁归 standard-gate,重复跑既慢又会让「哪条门禁在管什么」变糊。
    assert not any(
        command.startswith("bash scripts/check.sh") for command in commands
    )


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
    assert (
        "--mount type=tmpfs,destination=/var/lib/postgresql/data,tmpfs-size=4294967296"
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
    assert "hashFiles('backend/requirements.txt')" in cache["with"]["key"]
    assert "restore-keys" not in cache["with"]
    install = _named_step(job, "Install backend dependencies")
    assert install["env"] == {"HNSWLIB_NO_NATIVE": "1"}
    assert install["run"] == "python -m pip install -r backend/requirements.txt"

    timings = _named_step(job, "Upload PostgreSQL test timings")
    assert timings["if"] == "always()"
    assert timings["with"]["path"] == "backend/.local/postgres-junit.xml"

    run_commands = [
        step["run"]
        for step in job["steps"]
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    ]
    assert [command for command in run_commands if "scripts/check" in command] == [
        "bash scripts/check_postgres.sh"
    ]


@pytest.mark.parametrize("job_name", ["standard-backend", "standard-contracts", "postgres-integration"])
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
    assert install["run"] == "python -m pip install -r backend/requirements.txt"
    assert job["env"]["PIP_CACHE_DIR"] == "${{ github.workspace }}/.local/pip-portable-v1"
    cache = _uses_step(job, "actions/cache@0057852bfaa89a56745cba8c7296529d2fc39830")
    assert cache["with"] == {
        "path": "${{ env.PIP_CACHE_DIR }}",
        "key": "${{ runner.os }}-${{ runner.arch }}-py313-portable-hnsw-v1-${{ hashFiles('backend/requirements.txt') }}",
    }


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
